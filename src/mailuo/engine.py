"""Core Topic-Centric Session Manager (脉络核心引擎)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

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
    consolidated_themes: Optional[list[CoreTheme]] = None


class MailuoEngine:
    """围绕具体内容与核心主题的 Agent Session 管理器.

    核心能力：
    1. 自动路由用户输入（继续当前话题 / 隔离临时穿插问题 / 语义召回历史话题 / 开启新话题）
    2. 保持每个 TopicSession 上下文高度纯净（临时穿插提问标记为 ephemeral，不污染主线摘要）
    3. 随时通过自然语言『把上次讨论 XX 的话题调出来』恢复历史上下文
    4. 一键或自然语言触发『整理核心主题』，将零散 Session 归纳为用户专属的 CoreTheme
    """

    def __init__(
        self,
        db_path: str | Path = "~/.mailuo/mailuo.db",
        llm: Optional[LLMBackend] = None,
    ) -> None:
        self.storage = MailuoStorage(db_path=db_path)
        self.llm = llm or LLMBackend()

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
        # 将旧激活 session 设为 paused
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
            embedding=emb,
        )
        self.storage.save_session(sess)
        self.storage.set_active_session_id(sess.id)
        return sess

    # ------------------------------------------------------------------
    # 核心对话入口：自动分流、召回、隔离与整理
    # ------------------------------------------------------------------
    def chat(
        self,
        user_input: str,
        force_tangent: bool = False,
        force_new_topic: bool = False,
    ) -> TurnResult:
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
            lines = [f"已将您的所有历史话题整理归纳为 **{len(themes)} 个核心主题 (Core Themes)**：\n"]
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
            return TurnResult(
                decision=decision,
                active_session=target_sess,
                reply=reply,
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
                is_ephemeral=is_tangent,
            )
        )

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
        )

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
        """把所有零散的 TopicSession 整理成与用户有关的最核心主题 (Core Themes)."""
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
        """将两个讨论同一具体内容的碎片 Session 合并."""
        src = self.storage.get_session(source_session_id)
        dst = self.storage.get_session(target_session_id)
        if not src or not dst:
            raise ValueError("源 Session 或目标 Session 不存在")

        # 迁移非临时消息
        src_msgs = self.storage.get_session_messages(src.id, include_ephemeral=False)
        for m in src_msgs:
            self.storage.add_message(
                MessageRecord(
                    session_id=dst.id,
                    role=m.role,
                    content=m.content,
                    is_ephemeral=False,
                    metadata={"merged_from": src.id},
                )
            )

        # 合并标签、结论与待办
        dst.tags = list(dict.fromkeys(dst.tags + src.tags))
        dst.key_decisions = list(dict.fromkeys(dst.key_decisions + src.key_decisions))
        dst.open_questions = list(dict.fromkeys(dst.open_questions + src.open_questions))

        src.status = SessionStatus.MERGED
        self.storage.save_session(src)

        all_dst_msgs = self.storage.get_session_messages(dst.id, include_ephemeral=False)
        dst = self.llm.update_session_state(dst, all_dst_msgs)
        self.storage.save_session(dst)
        self.storage.set_active_session_id(dst.id)
        return dst
