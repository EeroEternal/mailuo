"""Core Topic-Centric Session Manager (脉络核心引擎 — 支持跨 pi.dev 与 Google agy 协同)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .adapters import AgentExecutionResult, GoogleAgyBridge, PiDevBridge
from .llm import LLMBackend
from .models import (
    CoreTheme,
    MessageRecord,
    RouteActionType,
    RouteDecision,
    SessionStatus,
    TopicSession,
)
from .storage import MailuoStorage


@dataclass
class TurnResult:
    """单轮对话执行后的完整脉络结果."""

    decision: RouteDecision
    active_session: Optional[TopicSession]
    reply: str
    agent_backend: str = "mailuo"
    agent_execution: Optional[AgentExecutionResult] = None
    consolidated_themes: Optional[list[CoreTheme]] = None


class MailuoEngine:
    """围绕具体内容与核心主题的多 Agent Session 管理器.

    核心能力：
    1. 自动路由用户输入（继续当前话题 / 隔离临时穿插问题 / 语义召回历史话题 / 开启新话题）
    2. 支持直接拉起外部 Agent（`pi.dev` 的 `pi` CLI 或 Google Antigravity 的 `agy` CLI），
       并按脉络逻辑管理它们的 `--session-id` / `--no-session` / `--conversation`
    3. 支持一键从本地磁盘同步导入 `~/.pi/agent/sessions` 和 `~/.gemini/antigravity-cli/brain` 的历史对话，
       统一整理为用户最核心的主题 (`CoreTheme`)
    """

    def __init__(
        self,
        db_path: str | Path = "d1",
        user_id: Optional[str] = None,
        llm: Optional[LLMBackend] = None,
        pi_bridge: Optional[PiDevBridge] = None,
        agy_bridge: Optional[GoogleAgyBridge] = None,
    ) -> None:
        self.storage = MailuoStorage(db_path=db_path, user_id=user_id)
        self.llm = llm or LLMBackend()
        self.pi_bridge = pi_bridge or PiDevBridge()
        self.agy_bridge = agy_bridge or GoogleAgyBridge()

    # ------------------------------------------------------------------
    # 当前激活 Session 管理
    # ------------------------------------------------------------------
    def get_active_session(self) -> Optional[TopicSession]:
        sid = self.storage.get_active_session_id()
        if not sid:
            return None
        return self.storage.get_session(sid)

    def switch_session(self, session_id: str) -> TopicSession:
        sess = self.storage.get_session(session_id)
        if not sess:
            raise ValueError(f"未找到 ID 为 {session_id} 的话题 Session")
        current = self.get_active_session()
        if current and current.id != sess.id and current.status == SessionStatus.ACTIVE:
            current.status = SessionStatus.PAUSED
            self.storage.save_session(current)

        sess.status = SessionStatus.ACTIVE
        self.storage.save_session(sess)
        self.storage.set_active_session_id(sess.id)
        return sess

    def create_topic_session(
        self,
        title: str,
        tags: Optional[list[str]] = None,
        parent_session_id: Optional[str] = None,
        external_sessions: Optional[dict[str, str]] = None,
    ) -> TopicSession:
        current = self.get_active_session()
        if current and current.status == SessionStatus.ACTIVE:
            current.status = SessionStatus.PAUSED
            self.storage.save_session(current)

        emb = self.llm.embed(f"{title} {' '.join(tags or [])}")
        sess = TopicSession(
            title=title,
            tags=tags or [],
            parent_session_id=parent_session_id or (current.id if current else None),
            status=SessionStatus.ACTIVE,
            external_sessions=external_sessions or {},
            embedding=emb,
        )
        self.storage.save_session(sess)
        self.storage.set_active_session_id(sess.id)
        return sess

    # ------------------------------------------------------------------
    # 核心对话入口：自动分流、召回、隔离、拉起外部 Agent (pi / agy) 与整理
    # ------------------------------------------------------------------
    def chat(
        self,
        user_input: str,
        agent_backend: str = "mailuo",
        interactive_agent: bool = False,
        force_tangent: bool = False,
        force_new_topic: bool = False,
        cwd: Optional[str | Path] = None,
    ) -> TurnResult:
        """执行一轮对话.

        参数:
            agent_backend: "mailuo" (内置引擎) | "pi" (拉起 pi.dev) | "agy" (拉起 Google Antigravity)
            interactive_agent: 若为 True 且指定了 pi/agy，则直接拉起其交互式 TUI 会话并在退出后同步记录
        """
        active_sess = self.get_active_session()

        if force_tangent and active_sess is not None:
            decision = RouteDecision(
                action=RouteActionType.SIDE_TANGENT,
                target_session_id=active_sess.id,
                reason=f"用户显式指定临时穿插提问（Side-Tangent），隔离于《{active_sess.title}》之外",
            )
        elif force_new_topic:
            title, tags = self.llm._extract_title_and_tags(user_input)
            decision = RouteDecision(
                action=RouteActionType.NEW_TOPIC,
                new_topic_title=title,
                new_topic_tags=tags,
                reason="用户显式开启新话题 Session",
            )
        else:
            decision = self.llm.route_user_input(user_input, active_sess, self.storage)

        # 分支 1：整理核心主题 (CONSOLIDATE)
        if decision.action == RouteActionType.CONSOLIDATE:
            themes = self.consolidate_themes()
            lines = [f"已将您的所有跨 Agent 历史话题整理归纳为 **{len(themes)} 个核心主题 (Core Themes)**：\n"]
            for idx, th in enumerate(themes, 1):
                lines.append(
                    f"### {idx}. {th.name} (`{th.id}`)\n"
                    f"- **关联具体话题数**: {len(th.session_ids)} 个 (`{', '.join(th.session_ids)}`)\n"
                    f"- **跨会话核心脉络**: {th.consolidated_insight}\n"
                )
            return TurnResult(
                decision=decision,
                active_session=active_sess,
                reply="\n".join(lines),
                agent_backend=agent_backend,
                consolidated_themes=themes,
            )

        # 分支 2：召回并切换到历史话题 (SWITCH_EXISTING)
        if decision.action == RouteActionType.SWITCH_EXISTING and decision.target_session_id:
            target_sess = self.switch_session(decision.target_session_id)
            theme = (
                self.storage.get_theme(target_sess.theme_id)
                if target_sess.theme_id
                else None
            )
            recent_msgs = self.storage.get_session_messages(
                target_sess.id, include_ephemeral=False, limit=8
            )
            reply = self.llm.generate_reply(
                user_input=user_input,
                session=target_sess,
                recent_messages=recent_msgs,
                theme=theme,
                is_tangent=False,
                recalled_from_search=True,
            )
            # 如果用户要求切回历史话题同时直接拉起交互式 pi 或 agy
            exec_res: Optional[AgentExecutionResult] = None
            if interactive_agent and agent_backend in ("pi", "agy"):
                exec_res = self._invoke_external_agent(
                    agent_backend=agent_backend,
                    session=target_sess,
                    prompt="请基于当前恢复的脉络上下文继续我们的讨论。",
                    theme=theme,
                    recent_messages=recent_msgs,
                    is_tangent=False,
                    interactive=True,
                    cwd=cwd,
                )
                self.storage.save_session(target_sess)
            return TurnResult(
                decision=decision,
                active_session=target_sess,
                reply=reply,
                agent_backend=agent_backend,
                agent_execution=exec_res,
            )

        # 分支 3：开启新话题 (NEW_TOPIC)
        if decision.action == RouteActionType.NEW_TOPIC or active_sess is None:
            title = decision.new_topic_title or user_input[:20]
            tags = decision.new_topic_tags
            active_sess = self.create_topic_session(title=title, tags=tags)

        # 分支 4：临时穿插提问 (SIDE_TANGENT) vs 正常推进主话题 (CONTINUE)
        is_tangent = decision.action == RouteActionType.SIDE_TANGENT
        recent_msgs = self.storage.get_session_messages(
            active_sess.id, include_ephemeral=False, limit=8
        )
        theme = (
            self.storage.get_theme(active_sess.theme_id)
            if active_sess.theme_id
            else None
        )

        # 记录用户消息（如果是穿插提问，is_ephemeral=True，绝不污染主线）
        self.storage.add_message(
            MessageRecord(
                session_id=active_sess.id,
                role="user",
                content=user_input,
                agent_source="user",
                is_ephemeral=is_tangent,
            )
        )

        exec_res = None
        if agent_backend in ("pi", "agy"):
            exec_res = self._invoke_external_agent(
                agent_backend=agent_backend,
                session=active_sess,
                prompt=user_input,
                theme=theme,
                recent_messages=recent_msgs,
                is_tangent=is_tangent,
                interactive=interactive_agent,
                cwd=cwd,
            )
            reply = exec_res.reply
            self.storage.save_session(active_sess)
        else:
            reply = self.llm.generate_reply(
                user_input=user_input,
                session=active_sess,
                recent_messages=recent_msgs,
                theme=theme,
                is_tangent=is_tangent,
                recalled_from_search=False,
            )

        self.storage.add_message(
            MessageRecord(
                session_id=active_sess.id,
                role="assistant",
                content=reply,
                agent_source=agent_backend,
                is_ephemeral=is_tangent,
            )
        )

        # 只有非穿插的正向讨论才更新主话题的结构化摘要
        if not is_tangent:
            all_clean_msgs = self.storage.get_session_messages(
                active_sess.id, include_ephemeral=False
            )
            active_sess = self.llm.update_session_state(active_sess, all_clean_msgs)
            self.storage.save_session(active_sess)

        return TurnResult(
            decision=decision,
            active_session=active_sess,
            reply=reply,
            agent_backend=agent_backend,
            agent_execution=exec_res,
        )

    def launch_agent_interactive(
        self,
        agent_backend: str,
        initial_prompt: Optional[str] = None,
        cwd: Optional[str | Path] = None,
    ) -> Optional[TopicSession]:
        """直接拉起 `pi` 或 `agy` 交互式界面（用于与 Agent 讨论具体话题，而不是把 pi/agy 当作话题名！）.

        - 如果当前已有激活的具体话题 Session：在该话题脉络下拉起 `pi` 或 `agy`；
        - 如果当前还没有激活的话题：创建一个待定标题的空白容器拉起纯净的 `pi` 或 `agy`；
          等用户在 `pi` / `agy` 里聊完退出后，根据用户在里面说的第一句话自动生成真实的话题标题与脉络摘要！
        """
        active_sess = self.get_active_session()
        is_placeholder = False

        if active_sess is None:
            is_placeholder = True
            active_sess = TopicSession(
                title="未命名新话题",
                tags=[],
                status=SessionStatus.ACTIVE,
            )

        theme = (
            self.storage.get_theme(active_sess.theme_id)
            if active_sess.theme_id
            else None
        )
        recent_msgs = (
            self.storage.get_session_messages(active_sess.id, include_ephemeral=False, limit=8)
            if not is_placeholder
            else []
        )

        exec_res = self._invoke_external_agent(
            agent_backend=agent_backend,
            session=active_sess,
            prompt=initial_prompt,
            theme=theme,
            recent_messages=recent_msgs,
            is_tangent=False,
            interactive=True,
            cwd=cwd,
        )

        captured = exec_res.captured_messages or []
        if not captured and is_placeholder and not initial_prompt:
            # 用户打开了交互式界面但一句话没说就直接退出了，不保存空垃圾 Session
            return None

        # 如果之前是未命名占位符，或者标题曾被误设为 "Google agy 专题讨论" / "pi.dev 专题讨论"
        if is_placeholder or active_sess.title in ("未命名新话题", "Google agy 专题讨论", "pi.dev 专题讨论"):
            first_user_text = initial_prompt or next(
                (m["content"] for m in captured if m["role"] == "user"),
                "",
            )
            if first_user_text:
                title, tags = self.llm._extract_title_and_tags(first_user_text)
                active_sess.title = title
                active_sess.tags = list(dict.fromkeys(active_sess.tags + tags))

        # 将交互式会话中捕获的新消息同步写入脉络库
        existing_contents = {m.content for m in recent_msgs}
        for m in captured:
            if m["content"] not in existing_contents:
                self.storage.add_message(
                    MessageRecord(
                        session_id=active_sess.id,
                        role=m["role"],
                        content=m["content"],
                        agent_source=agent_backend if m["role"] == "assistant" else "user",
                        is_ephemeral=False,
                    )
                )

        all_clean_msgs = self.storage.get_session_messages(active_sess.id, include_ephemeral=False)
        active_sess = self.llm.update_session_state(active_sess, all_clean_msgs)
        self.storage.save_session(active_sess)
        self.storage.set_active_session_id(active_sess.id)
        return active_sess

    def _invoke_external_agent(
        self,
        agent_backend: str,
        session: TopicSession,
        prompt: Optional[str],
        theme: Optional[CoreTheme],
        recent_messages: list[MessageRecord],
        is_tangent: bool,
        interactive: bool,
        cwd: Optional[str | Path],
    ) -> AgentExecutionResult:
        if agent_backend == "pi":
            return self.pi_bridge.run_turn(
                session=session,
                prompt=prompt,
                theme=theme,
                recent_messages=recent_messages,
                is_tangent=is_tangent,
                interactive=interactive,
                cwd=cwd,
            )
        if agent_backend == "agy":
            return self.agy_bridge.run_turn(
                session=session,
                prompt=prompt,
                theme=theme,
                recent_messages=recent_messages,
                is_tangent=is_tangent,
                interactive=interactive,
                cwd=cwd,
            )
        raise ValueError(f"不支持的外部 Agent 类型: {agent_backend}")

    # ------------------------------------------------------------------
    # 扫描并导入本地已有的 pi.dev 与 Google agy 原生会话记录
    # ------------------------------------------------------------------
    def sync_external_sessions(
        self,
        source: str = "all",
        limit_per_source: int = 10,
    ) -> list[TopicSession]:
        """从 `~/.pi/agent/sessions` 和 `~/.gemini/antigravity-cli/brain` 导入原生会话并按脉络逻辑生成结构化 TopicSession."""
        existing_sessions = self.storage.list_sessions(include_merged=True)
        known_ext_ids: dict[str, TopicSession] = {}
        for s in existing_sessions:
            for agent_k, ext_id in s.external_sessions.items():
                known_ext_ids[f"{agent_k}:{ext_id}"] = s

        imported: list[TopicSession] = []

        # 1. 同步 pi.dev 会话
        if source in ("all", "pi"):
            for fpath in self.pi_bridge.list_recent_session_files(limit=limit_per_source):
                parsed = self.pi_bridge.parse_pi_jsonl(fpath)
                ext_id = parsed["external_session_id"]
                msgs = parsed["messages"]
                if not msgs:
                    continue
                key = f"pi:{ext_id}"
                if key in known_ext_ids:
                    continue

                first_user_msg = next(
                    (m["content"] for m in msgs if m["role"] == "user"),
                    "pi.dev 导入会话",
                )
                title, tags = self.llm._extract_title_and_tags(first_user_msg)
                if "pi.dev" not in tags:
                    tags.append("pi.dev")

                sess = self.create_topic_session(
                    title=title,
                    tags=tags,
                    external_sessions={"pi": ext_id},
                )
                for m in msgs:
                    self.storage.add_message(
                        MessageRecord(
                            session_id=sess.id,
                            role=m["role"],
                            content=m["content"],
                            agent_source="pi",
                            is_ephemeral=False,
                        )
                    )
                clean_records = self.storage.get_session_messages(sess.id)
                sess = self.llm.update_session_state(sess, clean_records)
                self.storage.save_session(sess)
                imported.append(sess)

        # 2. 同步 Google Antigravity (agy) 会话
        if source in ("all", "agy"):
            for fpath in self.agy_bridge.list_recent_transcripts(limit=limit_per_source):
                parsed = self.agy_bridge.parse_agy_transcript(fpath)
                ext_id = parsed["external_session_id"]
                msgs = parsed["messages"]
                if not msgs:
                    continue
                key = f"agy:{ext_id}"
                if key in known_ext_ids:
                    continue

                first_user_msg = next(
                    (m["content"] for m in msgs if m["role"] == "user"),
                    "Google AGY 导入会话",
                )
                title, tags = self.llm._extract_title_and_tags(first_user_msg)
                if "agy" not in tags:
                    tags.append("agy")

                sess = self.create_topic_session(
                    title=title,
                    tags=tags,
                    external_sessions={"agy": ext_id},
                )
                for m in msgs:
                    self.storage.add_message(
                        MessageRecord(
                            session_id=sess.id,
                            role=m["role"],
                            content=m["content"],
                            agent_source="agy",
                            is_ephemeral=False,
                        )
                    )
                clean_records = self.storage.get_session_messages(sess.id)
                sess = self.llm.update_session_state(sess, clean_records)
                self.storage.save_session(sess)
                imported.append(sess)

        return imported

    # ------------------------------------------------------------------
    # 话题检索与核心主题归纳 API
    # ------------------------------------------------------------------
    def recall_by_query(self, query: str) -> Optional[TopicSession]:
        """根据关键词或自然语言描述找到最匹配的 Session 并自动切换过去."""
        results = self.storage.search_sessions(
            query=query,
            query_embedding=self.llm.embed(query),
            top_k=1,
        )
        if not results or results[0][1] <= 0.0:
            return None
        best_sess, _ = results[0]
        return self.switch_session(best_sess.id)

    def consolidate_themes(self) -> list[CoreTheme]:
        """把所有零散的 TopicSession (无论来自 pi、agy 还是内置对话) 整理成与用户有关的最核心主题."""
        sessions = self.storage.list_sessions(include_merged=False)
        if not sessions:
            return []
        self.storage.clear_themes()
        themes = self.llm.consolidate_core_themes(sessions)
        for th in themes:
            self.storage.save_theme(th)
            for sid in th.session_ids:
                sess = self.storage.get_session(sid)
                if sess:
                    sess.theme_id = th.id
                    self.storage.save_session(sess)
        return themes

    def merge_sessions(
        self, source_session_id: str, target_session_id: str
    ) -> TopicSession:
        """将两个讨论同一具体内容的碎片 Session 合并（保留多 Agent 绑定关系）."""
        src = self.storage.get_session(source_session_id)
        dst = self.storage.get_session(target_session_id)
        if not src or not dst:
            raise ValueError("源 Session 或目标 Session 不存在")

        src_msgs = self.storage.get_session_messages(src.id, include_ephemeral=False)
        for m in src_msgs:
            self.storage.add_message(
                MessageRecord(
                    session_id=dst.id,
                    role=m.role,
                    content=m.content,
                    agent_source=m.agent_source,
                    is_ephemeral=False,
                    metadata={"merged_from": src.id},
                )
            )

        dst.tags = list(dict.fromkeys(dst.tags + src.tags))
        dst.key_decisions = list(dict.fromkeys(dst.key_decisions + src.key_decisions))
        dst.open_questions = list(dict.fromkeys(dst.open_questions + src.open_questions))
        dst.external_sessions = {**src.external_sessions, **dst.external_sessions}

        src.status = SessionStatus.MERGED
        self.storage.save_session(src)

        all_dst_msgs = self.storage.get_session_messages(dst.id, include_ephemeral=False)
        dst = self.llm.update_session_state(dst, all_dst_msgs)
        self.storage.save_session(dst)
        self.storage.set_active_session_id(dst.id)
        return dst
