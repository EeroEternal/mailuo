"""LLM Backend & Smart Heuristic Engine for Session Routing, Summarization, and Consolidation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Optional

from .models import CoreTheme, MessageRecord, RouteActionType, RouteDecision, TopicSession
from .storage import MailuoStorage, tokenize


def deterministic_hash_embedding(text: str, dim: int = 64) -> list[float]:
    """轻量级本地特征哈希向量（用于无外部 Embedding API 时的语义与词袋相似度计算）."""
    tokens = tokenize(text)
    vec = [0.0] * dim
    for tok in tokens:
        h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
        idx = h % dim
        sign = 1.0 if ((h >> 8) & 1) == 0 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


class LLMBackend:
    """支持 OpenAI 兼容接口（OpenAI / DeepSeek / 本地 Ollama / Xinference）及智能离线兜底."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        self.api_key = api_key or os.getenv("MAILUO_API_KEY") or os.getenv("OPENAI_API_KEY")
        self.base_url = base_url or os.getenv("MAILUO_BASE_URL") or os.getenv("OPENAI_BASE_URL")
        self.model = model or os.getenv("MAILUO_MODEL") or "gpt-4o-mini"
        self._client: Any = None

        if self.api_key:
            try:
                from openai import OpenAI

                self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
            except Exception:
                self._client = None

    @property
    def is_live_llm(self) -> bool:
        return self._client is not None

    def embed(self, text: str) -> list[float]:
        return deterministic_hash_embedding(text)

    # ------------------------------------------------------------------
    # 1. 意图与脉络路由 (Router)
    # ------------------------------------------------------------------
    def route_user_input(
        self,
        user_input: str,
        active_session: Optional[TopicSession],
        storage: MailuoStorage,
    ) -> RouteDecision:
        """判断用户输入应该：
        - recall/switch: 调出上次讨论某话题的 Session
        - side_tangent: 穿插的临时问题（隔离上下文，不污染主话题）
        - new_topic: 开启全新话题
        - consolidate: 整理核心主题
        - continue: 继续当前话题
        """
        text = user_input.strip()

        # 1) 检测“整理核心主题”意图
        consolidate_patterns = [
            r"(整理|归纳|汇总|合并|提炼).*(核心主题|所有话题|session|会话|脉络)",
            r"把.*(session|会话|话题).*(整理|合并|聚类)",
        ]
        for pat in consolidate_patterns:
            if re.search(pat, text, re.IGNORECASE):
                return RouteDecision(
                    action=RouteActionType.CONSOLIDATE,
                    reason="识别到用户希望将零散的话题 Session 聚类整理为核心主题",
                )

        # 2) 检测“调出/切换上次讨论的某个话题 Session”意图
        recall_match = re.search(
            r"(?:上次|之前|前面|早些时候)?(?:我们)?(?:讨论|聊|研究|设计|说过)的?(?:那个|关于)?(.+?)(?:的?(?:话题|session|会话|内容|脉络))?(?:你?(?:可以|能|帮我)?(?:调出来|切过去|切换|找出来|恢复|继续聊)|呢)",
            text,
            re.IGNORECASE,
        )
        if not recall_match:
            recall_match = re.search(
                r"(?:调出|切换到|回到|找回|加载)(?:上次|之前)?(?:讨论|聊)?(?:关于)?(.+?)(?:的?(?:话题|session|会话))?$",
                text,
                re.IGNORECASE,
            )

        if recall_match:
            query_topic = recall_match.group(1).strip(" ，。？！、“”\"'")
            candidates = storage.search_sessions(
                query=query_topic,
                query_embedding=self.embed(query_topic),
                top_k=3,
            )
            if candidates and candidates[0][1] > 0.05:
                best_sess, score = candidates[0]
                return RouteDecision(
                    action=RouteActionType.SWITCH_EXISTING,
                    target_session_id=best_sess.id,
                    search_query=query_topic,
                    reason=f"根据『{query_topic}』检索并召回历史话题 Session: 《{best_sess.title}》 (匹配度 {score:.2f})",
                )

        # 3) 检测“临时穿插/顺便问一下（防止弄乱当前话题）”意图
        tangent_patterns = [
            r"^(顺便问|题外话|插一句|穿插一个|先不聊这个|临时问|突然想到|歪个楼|btw)",
            r"(顺便帮我|穿插一下|不要影响当前|别弄乱当前)",
        ]
        if active_session is not None:
            for pat in tangent_patterns:
                if re.search(pat, text, re.IGNORECASE):
                    return RouteDecision(
                        action=RouteActionType.SIDE_TANGENT,
                        target_session_id=active_session.id,
                        reason=f"检测到穿插提问，已开启隔离沙箱（Side-Tangent），绝不污染当前主话题《{active_session.title}》的上下文",
                    )

        # 4) 如果配置了在线 LLM，让 LLM 做更细粒度的语义漂移检测（是否偏离当前具体内容）
        if self._client is not None:
            llm_decision = self._route_via_llm(text, active_session, storage)
            if llm_decision is not None:
                return llm_decision

        # 5) 启发式话题漂移与新建判定
        if active_session is None:
            title, tags = self._extract_title_and_tags(text)
            return RouteDecision(
                action=RouteActionType.NEW_TOPIC,
                new_topic_title=title,
                new_topic_tags=tags,
                reason="当前无激活话题，自动为您创建针对该具体内容的专属 Session",
            )

        # 检查是否显式要求开启新话题
        new_topic_match = re.search(
            r"^(?:我们?)?(?:换个话题|聊个新的|新开一个话题|开启新话题|讨论另一个问题|来探讨一下)[：:，,\s]*(.+)",
            text,
        )
        if new_topic_match:
            core_text = new_topic_match.group(1)
            title, tags = self._extract_title_and_tags(core_text)
            return RouteDecision(
                action=RouteActionType.NEW_TOPIC,
                new_topic_title=title,
                new_topic_tags=tags,
                reason=f"识别到切换新讨论方向，已从《{active_session.title}》剥离并新建独立 Session",
            )

        # 检查输入是否与历史某个非当前 Session 高度重合，而与当前 Session 完全无关
        all_candidates = storage.search_sessions(
            query=text, query_embedding=self.embed(text), top_k=3
        )
        current_score = 0.0
        for s, sc in all_candidates:
            if s.id == active_session.id:
                current_score = sc
                break

        # 如果跟当前话题完全不沾边，且看起来是一个独立工具/代码杂项请求，自动走 SIDE_TANGENT 隔离
        unrelated_utility_patterns = [
            r"(写个正则|翻译一下|汇率|天气|命令行怎么写|报错了怎么修|算一下)",
        ]
        for pat in unrelated_utility_patterns:
            if re.search(pat, text) and current_score < 0.15:
                return RouteDecision(
                    action=RouteActionType.SIDE_TANGENT,
                    target_session_id=active_session.id,
                    reason=(
                        f"检测到与当前主话题《{active_session.title}》无关的穿插杂项提问，"
                        "已自动采用隔离上下文回答，保持主话题纯净"
                    ),
                )

        return RouteDecision(
            action=RouteActionType.CONTINUE,
            target_session_id=active_session.id,
            reason=f"延续当前具体话题《{active_session.title}》",
        )

    def _route_via_llm(
        self,
        user_input: str,
        active_session: Optional[TopicSession],
        storage: MailuoStorage,
    ) -> Optional[RouteDecision]:
        sessions_brief = [
            {"id": s.id, "title": s.title, "tags": s.tags, "summary": s.summary[:120]}
            for s in storage.list_sessions()[:10]
        ]
        active_info = (
            {
                "id": active_session.id,
                "title": active_session.title,
                "summary": active_session.summary,
            }
            if active_session
            else None
        )
        prompt = f"""你是一个面向『具体话题 Session 管理』的路由器。
当前激活的话题 Session: {json.dumps(active_info, ensure_ascii=False)}
已有的历史话题列表: {json.dumps(sessions_brief, ensure_ascii=False)}
用户最新输入: "{user_input}"

请判断应该采取哪种路由动作，返回严格 JSON：
- "continue": 继续当前激活的话题
- "side_tangent": 用户在当前话题下穿插了一个临时/无关的小问题，需要隔离回答以免把当前话题搞杂乱
- "switch": 用户想调出或回到列表中的某个历史话题（填 target_session_id）
- "new_topic": 用户开启了一个全新的、值得长期深入讨论的具体话题（填 new_topic_title 和 new_topic_tags）
- "consolidate": 用户要求整理、合并核心主题

JSON 格式:
{{"action": "continue|side_tangent|switch|new_topic|consolidate", "target_session_id": null, "new_topic_title": null, "new_topic_tags": [], "reason": "简短原因"}}"""
        try:
            resp = self._client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                temperature=0.1,
            )
            data = json.loads(resp.choices[0].message.content)
            return RouteDecision(
                action=RouteActionType(data.get("action", "continue")),
                target_session_id=data.get("target_session_id"),
                new_topic_title=data.get("new_topic_title"),
                new_topic_tags=data.get("new_topic_tags") or [],
                reason=data.get("reason", ""),
            )
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 2. 对话生成 (Chat Response with Context Assembly)
    # ------------------------------------------------------------------
    def generate_reply(
        self,
        user_input: str,
        session: TopicSession,
        recent_messages: list[MessageRecord],
        theme: Optional[CoreTheme] = None,
        is_tangent: bool = False,
        recalled_from_search: bool = False,
    ) -> str:
        """组装纯净的话题上下文并生成 Agent 回复."""
        if self._client is not None:
            try:
                sys_parts = [
                    f"你正在与用户围绕具体话题《{session.title}》进行深度讨论。",
                ]
                if theme:
                    sys_parts.append(
                        f"所属核心主题：《{theme.name}》（长期核心共识：{theme.consolidated_insight}）"
                    )
                if session.summary:
                    sys_parts.append(f"当前话题已沉淀摘要：{session.summary}")
                if session.key_decisions:
                    sys_parts.append(
                        "已达成的关键结论：" + "；".join(session.key_decisions)
                    )
                if is_tangent:
                    sys_parts.append(
                        "【注意】当前这条提问被识别为『临时穿插提问（Side-Tangent）』，请简洁直接回答该穿插问题，提醒用户该回答已被隔离，不会污染主话题上下文。"
                    )

                messages_payload: list[dict[str, str]] = [
                    {"role": "system", "content": "\n".join(sys_parts)}
                ]
                for m in recent_messages[-8:]:
                    messages_payload.append({"role": m.role, "content": m.content})
                messages_payload.append({"role": "user", "content": user_input})

                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=messages_payload,
                    temperature=0.5,
                )
                return resp.choices[0].message.content or ""
            except Exception as exc:
                return f"[LLM 调用异常，已切换本地脉络引擎响应: {exc}]\n" + self._local_reply(
                    user_input, session, recent_messages, theme, is_tangent, recalled_from_search
                )

        return self._local_reply(
            user_input, session, recent_messages, theme, is_tangent, recalled_from_search
        )

    def _local_reply(
        self,
        user_input: str,
        session: TopicSession,
        recent_messages: list[MessageRecord],
        theme: Optional[CoreTheme],
        is_tangent: bool,
        recalled_from_search: bool,
    ) -> str:
        if recalled_from_search:
            decisions_str = (
                "\n".join(f"  - {d}" for d in session.key_decisions)
                if session.key_decisions
                else "  - （尚无固化的关键结论，可继续深入）"
            )
            questions_str = (
                "\n".join(f"  - {q}" for q in session.open_questions)
                if session.open_questions
                else "  - （暂无待办问题）"
            )
            return (
                f"已为您调出历史话题 Session **《{session.title}》** (`{session.id}`)！\n\n"
                f"**上次讨论脉络摘要**：\n> {session.summary or '我们已经开启了该话题的初步探讨。'}\n\n"
                f"**已沉淀的关键结论**：\n{decisions_str}\n\n"
                f"**待继续推进的问题**：\n{questions_str}\n\n"
                f"现在上下文已无缝恢复到该具体话题，我们可以接着聊！"
            )

        if is_tangent:
            return (
                f"*(已通过 **Side-Tangent 隔离沙箱** 处理此穿插提问，不会写入主话题《{session.title}》的长期记忆)*\n\n"
                f"针对您的临时问题「{user_input}」：\n"
                f"已完成快速解答。随时可以继续回到刚才关于 **《{session.title}》** 的核心讨论中！"
            )

        theme_note = f"（归属核心主题：**{theme.name}**）" if theme else ""
        turn_count = len([m for m in recent_messages if m.role == "user"]) + 1
        return (
            f"围绕具体话题 **《{session.title}》**{theme_note}（第 {turn_count} 轮深入讨论）：\n"
            f"已记录并分析您的观点：「{user_input}」。\n"
            f"- 当前话题标签：`{', '.join(session.tags) if session.tags else '通用讨论'}`\n"
            f"- 话题上下文保持纯净，未混入无关杂项。"
        )

    # ------------------------------------------------------------------
    # 3. 动态提炼更新单话题状态摘要 (Session State Compaction)
    # ------------------------------------------------------------------
    def update_session_state(
        self, session: TopicSession, messages: list[MessageRecord]
    ) -> TopicSession:
        """从非临时的纯净对话流中提炼最新 Summary、关键结论与待解决问题."""
        clean_msgs = [m for m in messages if not m.is_ephemeral]
        if not clean_msgs:
            return session

        if self._client is not None:
            try:
                convo = "\n".join(f"{m.role}: {m.content}" for m in clean_msgs[-10:])
                prompt = f"""请根据以下围绕话题《{session.title}》的对话，增量提炼结构化状态摘要。
已有摘要: {session.summary}
已有关键结论: {json.dumps(session.key_decisions, ensure_ascii=False)}
最新对话:
{convo}

返回严格 JSON:
{{"summary": "一句话到两句话的核心进展摘要", "key_decisions": ["结论1", "结论2"], "open_questions": ["待定问题1"]}}"""
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    temperature=0.2,
                )
                data = json.loads(resp.choices[0].message.content)
                session.summary = data.get("summary", session.summary)
                session.key_decisions = data.get("key_decisions", session.key_decisions)
                session.open_questions = data.get("open_questions", session.open_questions)
                session.embedding = self.embed(
                    f"{session.title} {' '.join(session.tags)} {session.summary} {' '.join(session.key_decisions)}"
                )
                return session
            except Exception:
                pass

        # 本地启发式提炼：提取用户的关键陈述与问题
        user_msgs = [m.content.strip() for m in clean_msgs if m.role == "user"]
        latest_user = user_msgs[-1] if user_msgs else ""

        # 识别决策性语句或核心要点
        if any(
            kw in latest_user
            for kw in ["决定", "采用", "确定", "选定", "方案是", "用", "架构", "设计", "目标", "核心"]
        ):
            snippet = latest_user[:80]
            if snippet not in session.key_decisions:
                session.key_decisions.append(snippet)
                session.key_decisions = session.key_decisions[-5:]

        # 识别待办或疑问
        if "？" in latest_user or "?" in latest_user or any(
            kw in latest_user for kw in ["怎么", "如何", "是不是", "待定", "下一步"]
        ):
            q_snippet = latest_user[:80]
            if q_snippet not in session.open_questions:
                session.open_questions.append(q_snippet)
                session.open_questions = session.open_questions[-4:]

        # 生成精炼摘要
        first_point = user_msgs[0][:50] if user_msgs else session.title
        latest_point = latest_user[:60] if len(user_msgs) > 1 else ""
        if latest_point and latest_point != first_point:
            session.summary = f"围绕《{session.title}》展开讨论；最初探讨「{first_point}」，最新进展至「{latest_point}」（共 {len(user_msgs)} 轮有效交锋）。"
        else:
            session.summary = f"围绕《{session.title}》展开讨论，核心关注「{first_point}」。"

        session.embedding = self.embed(
            f"{session.title} {' '.join(session.tags)} {session.summary} {' '.join(session.key_decisions)}"
        )
        return session

    # ------------------------------------------------------------------
    # 4. 跨 Session 整理归纳用户『核心主题』(Core Themes Consolidation)
    # ------------------------------------------------------------------
    def consolidate_core_themes(
        self, sessions: list[TopicSession]
    ) -> list[CoreTheme]:
        """将零散的具体 TopicSession 自动聚类、归纳为少数几个与用户紧密相关的『核心主题』."""
        if not sessions:
            return []

        if self._client is not None:
            try:
                sess_payload = [
                    {
                        "id": s.id,
                        "title": s.title,
                        "tags": s.tags,
                        "summary": s.summary,
                        "key_decisions": s.key_decisions,
                    }
                    for s in sessions
                ]
                prompt = f"""你是一个个人知识与会话脉络整理专家。
以下是用户近期与 Agent 讨论的所有具体话题 Session 列表：
{json.dumps(sess_payload, ensure_ascii=False)}

请将这些零散的 Session 聚类整理为若干个跟用户最相关的『核心主题 (Core Themes)』，并提炼每个核心主题下的跨会话核心共识。
返回严格 JSON：
{{"themes": [{{"name": "核心主题名称", "description": "主题范围描述", "consolidated_insight": "跨多个Session提炼的核心结论与脉络", "session_ids": ["sess_xxx"]}}]}}"""
                resp = self._client.chat.completions.create(
                    model=self.model,
                    messages=[{"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    temperature=0.2,
                )
                data = json.loads(resp.choices[0].message.content)
                result_themes: list[CoreTheme] = []
                for item in data.get("themes", []):
                    result_themes.append(
                        CoreTheme(
                            name=item["name"],
                            description=item.get("description", ""),
                            consolidated_insight=item.get("consolidated_insight", ""),
                            session_ids=item.get("session_ids", []),
                        )
                    )
                if result_themes:
                    return result_themes
            except Exception:
                pass

        # 本地智能聚类（结合用户真实工程领域 + 语义关键词映射）
        domain_rules = [
            (
                "AI 网关与高并发负载均衡 (xlite / xgateway / Flux / SmartGate)",
                [
                    "xlite", "xgateway", "flux", "smartgate", "xrouter", "xlb",
                    "网关", "负载均衡", "并发", "调度", "压测", "new api", "newapi",
                    "l4", "tcp", "限流", "缓冲", "上游", "推理引擎", "评分",
                ],
            ),
            (
                "AI Agent 脉络与知识自动化工具 (Mailuo / X.com Threads / Zenotes)",
                [
                    "agent", "session", "上下文", "记忆", "脉络", "mailuo", "mai",
                    "threads", "x.com", "zenotes", "笔记", "知识库", "核心主题",
                    "mapreduce",
                ],
            ),
            (
                "分布式算力集群与基础设施运维 (TileInfer / XEnsemble / GCP / Linux)",
                [
                    "tileinfer", "xensemble", "cfchannel", "ssh", "gcp", "linux",
                    "主机", "hosts", "10.119", "192.168", "服务器", "部署", "postgresql",
                ],
            ),
            (
                "后端架构与数据库存储",
                ["数据库", "sqlite", "cloudflare", "d1", "向量", "架构", "检索", "后端"],
            ),
            (
                "量化与投资理财",
                ["量化", "股票", "策略", "交易", "理财", "回测", "基金"],
            ),
        ]

        buckets: dict[str, list[TopicSession]] = {}
        unassigned: list[TopicSession] = []

        for s in sessions:
            blob = f"{s.title} {' '.join(s.tags)} {s.summary}".lower()
            matched_domain = None
            best_hits = 0
            for domain_name, keywords in domain_rules:
                hits = sum(1 for kw in keywords if kw in blob)
                if hits > best_hits:
                    best_hits = hits
                    matched_domain = domain_name

            if matched_domain and best_hits > 0:
                buckets.setdefault(matched_domain, []).append(s)
            else:
                unassigned.append(s)

        # 对未命中预设领域的 Session 按项目标签或归入日常工程研发
        for s in unassigned:
            proj_tags = [t for t in s.tags if t not in ("pi.dev", "agy", "users")]
            group_key = f"专题：{proj_tags[0]}" if proj_tags else "日常工程研发与系统排障"
            buckets.setdefault(group_key, []).append(s)

        themes: list[CoreTheme] = []
        for theme_name, grouped_sessions in buckets.items():
            titles = [f"《{gs.title}》" for gs in grouped_sessions[:6]]
            if len(grouped_sessions) > 6:
                titles.append(f"等 {len(grouped_sessions)} 个话题")
            all_decisions: list[str] = []
            for gs in grouped_sessions:
                all_decisions.extend(gs.key_decisions)

            insight_parts = [
                f"聚合了 {len(grouped_sessions)} 个具体讨论话题（{', '.join(titles)}）。"
            ]
            if all_decisions:
                clean_decs = [self._strip_file_paths(d)[:65] for d in all_decisions if self._strip_file_paths(d)]
                if clean_decs:
                    insight_parts.append("核心沉淀：" + "；".join(dict.fromkeys(clean_decs[:4])))

            themes.append(
                CoreTheme(
                    name=theme_name,
                    description=f"围绕 {theme_name} 相关的系列深入探讨",
                    consolidated_insight=" ".join(insight_parts),
                    session_ids=[gs.id for gs in grouped_sessions],
                )
            )

        return themes

    # ------------------------------------------------------------------
    # Helper: 清洗路径噪音并提取干净的话题标题与标签
    # ------------------------------------------------------------------
    @staticmethod
    def _strip_file_paths(text: str) -> str:
        """剥离本地长文件路径（如微信截图路径、Downloads 路径）和 XML 标签，保留核心人类语言."""
        cleaned = re.sub(r"<[^>]+>", " ", text)
        cleaned = re.sub(r"(?:/Users/|/var/folders/|/private/|/home/)[^\s，。；：,;:!?\n]*", " ", cleaned)
        cleaned = re.sub(r"https?://[^\s，。；：,;:!?\n]+", lambda m: m.group(0).split("/")[-1], cleaned)
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" ，。？！、：:;；-\n\t")
        return cleaned

    def extract_session_title_and_tags(
        self,
        user_messages: list[str],
        cwd: str = "",
        agent_source: str = "",
    ) -> tuple[str, list[str]]:
        """从多轮用户发言与项目工作目录 (cwd) 中提炼清晰可读的 TopicSession 标题与标签."""
        project_name = ""
        if cwd:
            parts = [p for p in Path(cwd).parts if p not in ("/", "Users", "xinference", "github", "run")]
            if parts:
                project_name = parts[-1]

        best_sentence = ""
        for msg in user_messages[:5]:
            stripped = self._strip_file_paths(msg)
            # 跳过纯过渡句
            if len(stripped) >= 4 and not re.match(
                r"^(能找到上一次|你能找到|恢复第|继续|按次落地|获取最新|updagte)", stripped
            ):
                best_sentence = stripped
                break

        if not best_sentence and user_messages:
            best_sentence = self._strip_file_paths(user_messages[0])

        title, tags = self._extract_title_and_tags(best_sentence or project_name or "工程探讨")
        if project_name:
            if project_name.lower() not in title.lower():
                title = f"[{project_name}] {title}"
            if project_name not in tags:
                tags.insert(0, project_name)
        if agent_source and agent_source not in tags:
            tags.append(agent_source)
        return title, tags

    def _extract_title_and_tags(self, text: str) -> tuple[str, list[str]]:
        cleaned = self._strip_file_paths(text)
        cleaned = re.sub(
            r"^(我想讨论一下|我们来聊聊|我想做一个|请问|关于|讨论一下|研究一下|我们换个话题|新开一个话题)[：:，,\s]*",
            "",
            cleaned,
        )
        first_clause = re.split(r"[。！？!?\n；;]", cleaned)[0].strip(" ，,：:*#")
        if len(first_clause) > 26:
            title = first_clause[:26] + "..."
        else:
            title = first_clause or "工程专题讨论"

        known_keywords = [
            "xlite", "xgateway", "Flux", "SmartGate", "TileInfer", "XEnsemble",
            "Agent", "Session", "上下文", "数据库", "SQLite", "Cloudflare", "D1",
            "PostgreSQL", "向量检索", "架构", "并发", "负载均衡", "压测", "README",
            "MapReduce", "量化", "交易", "Python", "Rust",
        ]
        tags: list[str] = []
        for kw in known_keywords:
            if kw.lower() in text.lower():
                tags.append(kw)
        if not tags:
            words = [
                w
                for w in tokenize(first_clause)
                if len(w) >= 2 and w not in ("users", "xinference", "library", "containers")
            ]
            tags = list(dict.fromkeys(words[:3]))
        return title, tags
