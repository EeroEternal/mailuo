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

        # 1) Detect "consolidate core themes" intent
        consolidate_patterns = [
            r"(整理|归纳|汇总|合并|提炼).*(核心主题|所有话题|session|会话|脉络)",
            r"把.*(session|会话|话题).*(整理|合并|聚类)",
            r"(consolidate|cluster|summarize|organize).*(theme|session|topic)",
        ]
        for pat in consolidate_patterns:
            if re.search(pat, text, re.IGNORECASE):
                return RouteDecision(
                    action=RouteActionType.CONSOLIDATE,
                    reason="Detected request to consolidate scattered sessions into Core Themes",
                )

        # 2) Detect "recall/switch to previous topic session" intent
        recall_match = re.search(
            r"(?:上次|之前|前面|早些时候)?(?:我们)?(?:讨论|聊|研究|设计|说过)的?(?:那个|关于)?(.+?)(?:的?(?:话题|session|会话|内容|脉络))?(?:你?(?:可以|能|帮我)?(?:调出来|切过去|切换|找出来|恢复|继续聊)|呢)",
            text,
            re.IGNORECASE,
        )
        if not recall_match:
            recall_match = re.search(
                r"(?:调出|切换到|回到|找回|加载|recall|switch to|load)(?:上次|之前)?(?:讨论|聊)?(?:关于)?(.+?)(?:的?(?:话题|session|会话|topic))?$",
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
                    reason=f"Recalled existing topic session [{best_sess.title}] for query '{query_topic}' (score {score:.2f})",
                )

        # 3) Detect "side-tangent / ephemeral question" intent
        tangent_patterns = [
            r"^(顺便问|题外话|插一句|穿插一个|先不聊这个|临时问|突然想到|歪个楼|btw|by the way|quick question)",
            r"(顺便帮我|穿插一下|不要影响当前|别弄乱当前)",
        ]
        if active_session is not None:
            for pat in tangent_patterns:
                if re.search(pat, text, re.IGNORECASE):
                    return RouteDecision(
                        action=RouteActionType.SIDE_TANGENT,
                        target_session_id=active_session.id,
                        reason=f"Side-Tangent sandbox enabled; isolated from active topic [{active_session.title}]",
                    )

        # 4) If live LLM is configured, use LLM for semantic drift routing
        if self._client is not None:
            llm_decision = self._route_via_llm(text, active_session, storage)
            if llm_decision is not None:
                return llm_decision

        # 5) Heuristic topic drift and new topic detection
        if active_session is None:
            title, tags = self._extract_title_and_tags(text)
            return RouteDecision(
                action=RouteActionType.NEW_TOPIC,
                new_topic_title=title,
                new_topic_tags=tags,
                reason="No active topic session; created a new dedicated topic session",
            )

        new_topic_match = re.search(
            r"^(?:我们?)?(?:换个话题|聊个新的|新开一个话题|开启新话题|讨论另一个问题|来探讨一下|new topic)[：:，,\s]*(.+)",
            text,
            re.IGNORECASE,
        )
        if new_topic_match:
            core_text = new_topic_match.group(1)
            title, tags = self._extract_title_and_tags(core_text)
            return RouteDecision(
                action=RouteActionType.NEW_TOPIC,
                new_topic_title=title,
                new_topic_tags=tags,
                reason=f"Switched to a new topic direction from [{active_session.title}]",
            )

        all_candidates = storage.search_sessions(
            query=text, query_embedding=self.embed(text), top_k=3
        )
        current_score = 0.0
        for s, sc in all_candidates:
            if s.id == active_session.id:
                current_score = sc
                break

        unrelated_utility_patterns = [
            r"(写个正则|翻译一下|汇率|天气|命令行怎么写|报错了怎么修|算一下)",
        ]
        for pat in unrelated_utility_patterns:
            if re.search(pat, text) and current_score < 0.15:
                return RouteDecision(
                    action=RouteActionType.SIDE_TANGENT,
                    target_session_id=active_session.id,
                    reason=(
                        f"Detected unrelated utility question; isolated from active topic [{active_session.title}]"
                    ),
                )

        return RouteDecision(
            action=RouteActionType.CONTINUE,
            target_session_id=active_session.id,
            reason=f"Continuing active topic [{active_session.title}]",
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
        prompt = f"""You are a topic-centric session router.
Active Topic Session: {json.dumps(active_info, ensure_ascii=False)}
Existing Topic Sessions: {json.dumps(sessions_brief, ensure_ascii=False)}
Latest User Input: "{user_input}"

Choose the routing action and return strict JSON:
- "continue": Continue the active topic
- "side_tangent": Ephemeral side question that should be isolated so it does not pollute the active topic
- "switch": Recall/switch to an existing session from the list (provide target_session_id)
- "new_topic": Start a brand-new topic session (provide new_topic_title and new_topic_tags)
- "consolidate": Consolidate sessions into Core Themes

JSON format:
{{"action": "continue|side_tangent|switch|new_topic|consolidate", "target_session_id": null, "new_topic_title": null, "new_topic_tags": [], "reason": "brief reason in English"}}"""
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
    # 2. Chat Response with Context Assembly
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
        """Assemble clean topic context and generate Agent reply."""
        if self._client is not None:
            try:
                sys_parts = [
                    f"You are discussing the topic [{session.title}] with the user.",
                ]
                if theme:
                    sys_parts.append(
                        f"Core Theme: [{theme.name}] (Insight: {theme.consolidated_insight})"
                    )
                if session.summary:
                    sys_parts.append(f"Current Topic Summary: {session.summary}")
                if session.key_decisions:
                    sys_parts.append(
                        "Key Decisions: " + "; ".join(session.key_decisions)
                    )
                if is_tangent:
                    sys_parts.append(
                        "[NOTE] This question is an isolated Side-Tangent. Answer concisely without altering the main topic."
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
                return f"[LLM fallback to local engine: {exc}]\n" + self._local_reply(
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
                else "  - (No recorded decisions yet)"
            )
            questions_str = (
                "\n".join(f"  - {q}" for q in session.open_questions)
                if session.open_questions
                else "  - (No open questions)"
            )
            return (
                f"Recalled topic session **[{session.title}]** (`{session.id}`).\n\n"
                f"**Session Summary**:\n> {session.summary or 'Initial discussion started.'}\n\n"
                f"**Key Decisions**:\n{decisions_str}\n\n"
                f"**Open Questions**:\n{questions_str}\n\n"
                f"Context restored to this topic."
            )

        if is_tangent:
            return (
                f"*(Handled via **Side-Tangent Sandbox** — isolated from main topic [{session.title}])* \n\n"
                f"Regarding `{user_input}`:\n"
                f"Recorded as an ephemeral side-query. Main topic **[{session.title}]** remains clean."
            )

        theme_note = f" (Core Theme: **{theme.name}**)" if theme else ""
        turn_count = len([m for m in recent_messages if m.role == "user"]) + 1
        return (
            f"Topic **[{session.title}]**{theme_note} (Turn #{turn_count}):\n"
            f"Recorded input: `{user_input}`.\n"
            f"- Tags: `{', '.join(session.tags) if session.tags else 'general'}`\n"
            f"- Context state updated."
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
                prompt = f"""Incrementally refine the structured state summary for the topic "{session.title}".
Existing summary: {session.summary}
Existing key decisions: {json.dumps(session.key_decisions, ensure_ascii=False)}
Latest conversation:
{convo}

Return strict JSON in English:
{{"summary": "one or two sentences on the core progress", "key_decisions": ["decision 1", "decision 2"], "open_questions": ["open question 1"]}}"""
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

        # 深度分析完整对话流（结合用户核心诉求 + Agent 最终沉淀的技术方案/结论/PR/根因分析）
        user_msgs = [self._strip_file_paths(m.content) for m in clean_msgs if m.role == "user"]
        user_msgs = [u for u in user_msgs if u]
        asst_msgs = [m.content.strip() for m in clean_msgs if m.role == "assistant" and m.content.strip()]

        # 1. 从 Agent 最终回复和用户确认中提炼真正的技术决策与交付结论 (key_decisions)
        extracted_decisions: list[str] = []
        for am in asst_msgs[-4:]:
            # 提取 PR / Commit / 版本发布信息
            pr_matches = re.findall(r"(?:PR\s*#\d+|pull/\d+|v\d+\.\d+\.\d+|[0-9a-f]{7}\b)[^\n。；*]*", am)
            for pm in pr_matches[:2]:
                clean_pm = self._strip_file_paths(pm).strip(" -*•:：`")
                if len(clean_pm) >= 8 and clean_pm not in extracted_decisions:
                    extracted_decisions.append(clean_pm[:90])

            # 提取 Markdown 要点行或核心结论句
            for line in am.splitlines():
                sline = line.strip()
                if re.match(r"^(?:[-*•]|\d+\.)\s+\*\*.*?\*\*", sline) or any(
                    kw in sline for kw in ["核心原因", "根本原因", "核心结论", "已合入", "已推到", "已完成", "定位："]
                ):
                    cleaned_line = re.sub(r"[*`#>]", "", self._strip_file_paths(sline)).strip(" -•:")
                    if 10 <= len(cleaned_line) <= 110 and cleaned_line not in extracted_decisions:
                        extracted_decisions.append(cleaned_line)

        for um in user_msgs:
            if any(kw in um for kw in ["决定", "采用", "确定", "方案是", "定位", "用 rust", "对标"]):
                snippet = um[:85]
                if snippet not in extracted_decisions:
                    extracted_decisions.append(snippet)

        if extracted_decisions:
            session.key_decisions = list(dict.fromkeys(session.key_decisions + extracted_decisions))[-5:]

        # 2. 提取待推进问题或后续动作 (open_questions)
        for um in user_msgs[-3:]:
            if "？" in um or "?" in um or any(kw in um for kw in ["怎么", "如何", "是不是", "下一步", "全量ci"]):
                if len(um) >= 6 and um[:80] not in session.open_questions:
                    session.open_questions.append(um[:80])
                    session.open_questions = session.open_questions[-4:]

        # 3. 合成高信息密度的技术脉络摘要 (summary)
        meaningful_users = [
            u for u in user_msgs
            if len(u) >= 5 and not re.match(r"^(提交代码|获取最新|查看最新|能找到|你能找到|不要用)", u)
        ]
        core_topic_req = meaningful_users[0][:65] if meaningful_users else (user_msgs[0][:65] if user_msgs else session.title)
        outcome_note = ""
        if session.key_decisions:
            outcome_note = f"; key outcome: {session.key_decisions[-1][:75]}"
        elif asst_msgs:
            last_asst_clean = re.sub(r"[*`#>]", "", self._strip_file_paths(asst_msgs[-1]))
            first_asst_sentence = re.split(r"[。\n]", last_asst_clean)[0].strip()
            if first_asst_sentence:
                outcome_note = f"; latest progress: {first_asst_sentence[:75]}"

        session.summary = f"Focus: \"{core_topic_req}\"{outcome_note} ({len(user_msgs)} user turns)."

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
                prompt = f"""You organise a person's discussion history into core themes.
Below are all specific topic sessions the user recently discussed with agents:
{json.dumps(sess_payload, ensure_ascii=False)}

Cluster them into a few Core Themes that matter most to the user, and extract the cross-session insight of each theme. Write all values in English.
Return strict JSON:
{{"themes": [{{"name": "theme name", "description": "theme scope", "consolidated_insight": "cross-session conclusions and trajectory", "session_ids": ["sess_xxx"]}}]}}"""
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
                "AI gateways & high-concurrency load balancing (xlite / xgateway / Flux / SmartGate)",
                [
                    "xlite", "xgateway", "flux", "smartgate", "xrouter", "xlb",
                    "网关", "负载均衡", "并发", "调度", "压测", "new api", "newapi",
                    "l4", "tcp", "限流", "缓冲", "上游", "推理引擎", "评分",
                ],
            ),
            (
                "AI agent context & knowledge automation (Mailuo / X.com Threads / Zenotes)",
                [
                    "agent", "session", "上下文", "记忆", "脉络", "mailuo", "mai",
                    "threads", "x.com", "zenotes", "笔记", "知识库", "核心主题",
                    "mapreduce",
                ],
            ),
            (
                "Distributed compute clusters & infrastructure ops (TileInfer / XEnsemble / GCP / Linux)",
                [
                    "tileinfer", "xensemble", "cfchannel", "ssh", "gcp", "linux",
                    "主机", "hosts", "10.119", "192.168", "服务器", "部署", "postgresql",
                ],
            ),
            (
                "Backend architecture & data storage",
                ["database", "sqlite", "cloudflare", "d1", "vector", "architecture", "向量", "架构", "检索", "后端"],
            ),
            (
                "Quant trading & personal finance",
                ["quant", "stock", "strategy", "trading", "backtest", "量化", "股票", "策略", "交易", "理财", "回测", "基金"],
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
            group_key = f"Topic: {proj_tags[0]}" if proj_tags else "Everyday engineering & troubleshooting"
            buckets.setdefault(group_key, []).append(s)

        themes: list[CoreTheme] = []
        for theme_name, grouped_sessions in buckets.items():
            titles = [f'"{gs.title}"' for gs in grouped_sessions[:6]]
            if len(grouped_sessions) > 6:
                titles.append(f"and {len(grouped_sessions) - 6} more")
            all_decisions: list[str] = []
            for gs in grouped_sessions:
                all_decisions.extend(gs.key_decisions)

            insight_parts = [
                f"Groups {len(grouped_sessions)} discussion topics ({', '.join(titles)})."
            ]
            if all_decisions:
                clean_decs = [self._strip_file_paths(d)[:65] for d in all_decisions if self._strip_file_paths(d)]
                if clean_decs:
                    insight_parts.append("Key takeaways: " + "; ".join(dict.fromkeys(clean_decs[:4])))

            themes.append(
                CoreTheme(
                    name=theme_name,
                    description=f"Deep dives related to {theme_name}",
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

        title, tags = self._extract_title_and_tags(
            best_sentence or project_name or "Engineering discussion"
        )
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
            title = first_clause or "Engineering discussion"

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
