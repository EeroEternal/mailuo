"""Multi-Tenant Cloudflare Worker + D1 Storage Engine & User Auth for Mailuo (`mai`).

- 云端网关默认地址：`https://mailuo-api.lipeng-sh.workers.dev`（后端直连 Cloudflare D1 `mailuo-db`）
- 任何一台机器只需 `pip install` 后运行 `mai register` 或 `mai login`，无需在机器上装 `wrangler` 或配置 Cloudflare 密钥。
- 所有表 (`tenant_core_themes`、`tenant_topic_sessions`、`tenant_messages`、`tenant_app_state`) 均通过 `user_id` 严格做租户隔离，每个人的数据完全独立、绝不串号。
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any, Optional, Sequence

from .models import CoreTheme, MessageRecord, SessionStatus, TopicSession, utc_now_iso

DEFAULT_GATEWAY_URL = "https://mailuo-api.lipeng-sh.workers.dev"
AUTH_CONFIG_PATH = Path("~/.mailuo/auth.json").expanduser()
USER_ID_PLACEHOLDER = "__CURRENT_USER_ID__"


def tokenize(text: str) -> list[str]:
    """同时支持中英文的分词器（英文按词，中文按字与双字 Bigram 切分）."""
    text = text.lower()
    en_words = re.findall(r"[a-z0-9_]+", text)
    zh_chars = re.findall(r"[\u4e00-\u9fff]", text)
    zh_bigrams = [zh_chars[i] + zh_chars[i + 1] for i in range(len(zh_chars) - 1)]
    return en_words + zh_chars + zh_bigrams


def cosine_similarity(vec_a: list[float], vec_b: list[float]) -> float:
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot = sum(a * b for a, b in zip(vec_a, vec_b))
    norm_a = math.sqrt(sum(a * a for a in vec_a))
    norm_b = math.sqrt(sum(b * b for b in vec_b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


# ------------------------------------------------------------------
# 用户注册、登录与本地凭证管理 (`~/.mailuo/auth.json`)
# ------------------------------------------------------------------
def load_auth_profile(config_path: Path = AUTH_CONFIG_PATH) -> Optional[dict[str, Any]]:
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            if data.get("token") and data.get("username"):
                return data
        except Exception:
            pass
    env_token = os.getenv("MAILUO_TOKEN")
    if env_token:
        return {
            "user_id": os.getenv("MAILUO_USER_ID", "usr_f622c5bf43"),
            "username": os.getenv("MAILUO_USERNAME", "lipi"),
            "token": env_token,
            "gateway_url": os.getenv("MAILUO_GATEWAY_URL", DEFAULT_GATEWAY_URL),
        }
    return None


def save_auth_profile(profile: dict[str, Any], config_path: Path = AUTH_CONFIG_PATH) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(profile, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_auth_profile(config_path: Path = AUTH_CONFIG_PATH) -> None:
    if config_path.exists():
        config_path.unlink()


def _gateway_post(
    path: str,
    payload: dict[str, Any],
    token: Optional[str] = None,
    gateway_url: Optional[str] = None,
) -> dict[str, Any]:
    base = (gateway_url or os.getenv("MAILUO_GATEWAY_URL") or DEFAULT_GATEWAY_URL).rstrip("/")
    url = f"{base}{path}"
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "mailuo-cli/0.2.0",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw_err = e.read().decode("utf-8", errors="ignore")
        try:
            err_json = json.loads(raw_err)
            raise RuntimeError(err_json.get("error") or f"HTTP {e.code}") from e
        except json.JSONDecodeError:
            raise RuntimeError(f"HTTP {e.code}: {raw_err}") from e


def register_user(
    username: str,
    password: str,
    gateway_url: Optional[str] = None,
    config_path: Path = AUTH_CONFIG_PATH,
) -> dict[str, Any]:
    """Register a new user in Cloudflare D1 and save auth profile locally."""
    res = _gateway_post(
        "/auth/register",
        {"username": username, "password": password},
        gateway_url=gateway_url,
    )
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "Registration failed")
    profile = {
        "user_id": res["user_id"],
        "username": res["username"],
        "token": res["token"],
        "gateway_url": gateway_url or os.getenv("MAILUO_GATEWAY_URL") or DEFAULT_GATEWAY_URL,
    }
    save_auth_profile(profile, config_path=config_path)
    return profile


def login_user(
    username: str,
    password: str,
    gateway_url: Optional[str] = None,
    config_path: Path = AUTH_CONFIG_PATH,
) -> dict[str, Any]:
    """Log into an existing account and save auth profile locally."""
    res = _gateway_post(
        "/auth/login",
        {"username": username, "password": password},
        gateway_url=gateway_url,
    )
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "Login failed")
    profile = {
        "user_id": res["user_id"],
        "username": res["username"],
        "token": res["token"],
        "gateway_url": gateway_url or os.getenv("MAILUO_GATEWAY_URL") or DEFAULT_GATEWAY_URL,
    }
    save_auth_profile(profile, config_path=config_path)
    return profile


class CloudflareGatewayClient:
    """Multi-tenant Cloudflare Worker D1 client (isolated by user_id)."""

    def __init__(self, token: str, username: str, user_id: str, gateway_url: str = DEFAULT_GATEWAY_URL) -> None:
        self.token = token
        self.username = username
        self.user_id = user_id
        self.gateway_url = gateway_url.rstrip("/")

    def execute(self, sql: str, params: Optional[Sequence[Any]] = None) -> list[dict[str, Any]]:
        res = _gateway_post(
            "/d1/query",
            {"sql": sql, "params": list(params) if params is not None else []},
            token=self.token,
            gateway_url=self.gateway_url,
        )
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "Cloudflare D1 query failed")
        return res.get("results") or []


LOCAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS tenant_core_themes (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    consolidated_insight TEXT NOT NULL DEFAULT '',
    session_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_topic_sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    tags_json TEXT NOT NULL DEFAULT '[]',
    theme_id TEXT,
    parent_session_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    summary TEXT NOT NULL DEFAULT '',
    key_decisions_json TEXT NOT NULL DEFAULT '[]',
    open_questions_json TEXT NOT NULL DEFAULT '[]',
    external_sessions_json TEXT NOT NULL DEFAULT '{}',
    embedding_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_messages (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    agent_source TEXT NOT NULL DEFAULT 'mailuo',
    is_ephemeral INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tenant_app_state (
    user_id TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (user_id, key)
);
"""


class MailuoStorage:
    """Multi-user isolated persistence layer (Cloudflare D1 or local SQLite)."""

    def __init__(
        self,
        db_path: str | Path = "d1",
        user_id: Optional[str] = None,
    ) -> None:
        self.backend_mode = "sqlite"
        self._cloud: Optional[CloudflareGatewayClient] = None
        self._conn: Optional[sqlite3.Connection] = None
        self.user_id = user_id or "local_user"
        self.username = self.user_id

        if str(db_path) == "d1":
            profile = load_auth_profile()
            if profile and profile.get("token"):
                self.user_id = profile["user_id"]
                self.username = profile["username"]
                gw = profile.get("gateway_url") or DEFAULT_GATEWAY_URL
                self._cloud = CloudflareGatewayClient(
                    token=profile["token"],
                    username=self.username,
                    user_id=self.user_id,
                    gateway_url=gw,
                )
                self.backend_mode = "cloudflare-d1"
                self.db_label = f"Cloudflare D1 (user: {self.username})"
                return
            db_path = "~/.mailuo/mailuo.db"

        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_label = f"SQLite ({self.db_path.name} | user: {self.username})"
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_sqlite_schema()

    @property
    def is_authenticated_cloud(self) -> bool:
        return self._cloud is not None

    def _init_sqlite_schema(self) -> None:
        assert self._conn is not None
        cur = self._conn.cursor()
        cur.executescript(LOCAL_SCHEMA_SQL)
        self._conn.commit()

    def _uid_param(self) -> str:
        return USER_ID_PLACEHOLDER if self._cloud is not None else self.user_id

    def _query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        if self._cloud is not None:
            return self._cloud.execute(sql, params)
        assert self._conn is not None
        cur = self._conn.execute(sql, params)
        rows = cur.fetchall()
        self._conn.commit()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Active Session Pointer (Per User)
    # ------------------------------------------------------------------
    def get_active_session_id(self) -> Optional[str]:
        rows = self._query(
            "SELECT value FROM tenant_app_state WHERE user_id = ? AND key = 'active_session_id'",
            (self._uid_param(),),
        )
        return rows[0]["value"] if rows and rows[0].get("value") else None

    def set_active_session_id(self, session_id: Optional[str]) -> None:
        val = session_id or ""
        self._query(
            """
            INSERT INTO tenant_app_state (user_id, key, value) VALUES (?, 'active_session_id', ?)
            ON CONFLICT(user_id, key) DO UPDATE SET value = excluded.value
            """,
            (self._uid_param(), val),
        )

    # ------------------------------------------------------------------
    # TopicSession CRUD (Isolated by user_id)
    # ------------------------------------------------------------------
    def save_session(self, session: TopicSession) -> TopicSession:
        session.updated_at = utc_now_iso()
        self._query(
            """
            INSERT INTO tenant_topic_sessions (
                id, user_id, title, tags_json, theme_id, parent_session_id, status,
                summary, key_decisions_json, open_questions_json, external_sessions_json,
                embedding_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                tags_json = excluded.tags_json,
                theme_id = excluded.theme_id,
                parent_session_id = excluded.parent_session_id,
                status = excluded.status,
                summary = excluded.summary,
                key_decisions_json = excluded.key_decisions_json,
                open_questions_json = excluded.open_questions_json,
                external_sessions_json = excluded.external_sessions_json,
                embedding_json = COALESCE(excluded.embedding_json, tenant_topic_sessions.embedding_json),
                updated_at = excluded.updated_at
            WHERE tenant_topic_sessions.user_id = excluded.user_id
            """,
            (
                session.id,
                self._uid_param(),
                session.title,
                json.dumps(session.tags, ensure_ascii=False),
                session.theme_id,
                session.parent_session_id,
                session.status.value,
                session.summary,
                json.dumps(session.key_decisions, ensure_ascii=False),
                json.dumps(session.open_questions, ensure_ascii=False),
                json.dumps(session.external_sessions, ensure_ascii=False),
                json.dumps(session.embedding) if session.embedding else None,
                session.created_at,
                session.updated_at,
            ),
        )
        return session

    def get_session(self, session_id: str) -> Optional[TopicSession]:
        rows = self._query(
            "SELECT * FROM tenant_topic_sessions WHERE id = ? AND user_id = ?",
            (session_id, self._uid_param()),
        )
        return self._row_to_session(rows[0]) if rows else None

    def delete_session(self, session_id: str) -> None:
        self._query(
            "DELETE FROM tenant_messages WHERE session_id = ? AND user_id = ?",
            (session_id, self._uid_param()),
        )
        self._query(
            "DELETE FROM tenant_topic_sessions WHERE id = ? AND user_id = ?",
            (session_id, self._uid_param()),
        )
        if self.get_active_session_id() == session_id:
            self.set_active_session_id(None)

    def list_sessions(
        self, include_merged: bool = False, theme_id: Optional[str] = None
    ) -> list[TopicSession]:
        query = "SELECT * FROM tenant_topic_sessions WHERE user_id = ?"
        params: list[Any] = [self._uid_param()]
        if not include_merged:
            query += " AND status != ?"
            params.append(SessionStatus.MERGED.value)
        if theme_id is not None:
            query += " AND theme_id = ?"
            params.append(theme_id)
        query += " ORDER BY updated_at DESC"
        rows = self._query(query, tuple(params))
        return [self._row_to_session(r) for r in rows]

    def _row_to_session(self, row: dict[str, Any]) -> TopicSession:
        emb_raw = row.get("embedding_json")
        ext_raw = row.get("external_sessions_json") or "{}"
        return TopicSession(
            id=row["id"],
            title=row["title"],
            tags=json.loads(row.get("tags_json") or "[]"),
            theme_id=row.get("theme_id"),
            parent_session_id=row.get("parent_session_id"),
            status=SessionStatus(row.get("status") or "active"),
            summary=row.get("summary") or "",
            key_decisions=json.loads(row.get("key_decisions_json") or "[]"),
            open_questions=json.loads(row.get("open_questions_json") or "[]"),
            external_sessions=json.loads(ext_raw),
            embedding=json.loads(emb_raw) if emb_raw else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Message CRUD (Isolated by user_id)
    # ------------------------------------------------------------------
    def add_message(self, msg: MessageRecord) -> MessageRecord:
        self._query(
            """
            INSERT INTO tenant_messages (
                id, user_id, session_id, role, content, agent_source, is_ephemeral, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg.id,
                self._uid_param(),
                msg.session_id,
                msg.role,
                msg.content,
                msg.agent_source,
                1 if msg.is_ephemeral else 0,
                json.dumps(msg.metadata, ensure_ascii=False),
                msg.created_at,
            ),
        )
        if not msg.is_ephemeral:
            self._query(
                "UPDATE tenant_topic_sessions SET updated_at = ? WHERE id = ? AND user_id = ?",
                (utc_now_iso(), msg.session_id, self._uid_param()),
            )
        return msg

    def get_session_messages(
        self,
        session_id: str,
        include_ephemeral: bool = False,
        limit: Optional[int] = None,
    ) -> list[MessageRecord]:
        query = "SELECT * FROM tenant_messages WHERE session_id = ? AND user_id = ?"
        params: list[Any] = [session_id, self._uid_param()]
        if not include_ephemeral:
            query += " AND is_ephemeral = 0"
        query += " ORDER BY created_at ASC"
        rows = self._query(query, tuple(params))
        msgs = [
            MessageRecord(
                id=r["id"],
                session_id=r["session_id"],
                role=r["role"],
                content=r["content"],
                agent_source=r.get("agent_source") or "mailuo",
                is_ephemeral=bool(r.get("is_ephemeral")),
                metadata=json.loads(r.get("metadata_json") or "{}"),
                created_at=r["created_at"],
            )
            for r in rows
        ]
        if limit and len(msgs) > limit:
            return msgs[-limit:]
        return msgs

    # ------------------------------------------------------------------
    # CoreTheme CRUD (Isolated by user_id)
    # ------------------------------------------------------------------
    def save_theme(self, theme: CoreTheme) -> CoreTheme:
        theme.updated_at = utc_now_iso()
        self._query(
            """
            INSERT INTO tenant_core_themes (
                id, user_id, name, description, consolidated_insight, session_ids_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                consolidated_insight = excluded.consolidated_insight,
                session_ids_json = excluded.session_ids_json,
                updated_at = excluded.updated_at
            WHERE tenant_core_themes.user_id = excluded.user_id
            """,
            (
                theme.id,
                self._uid_param(),
                theme.name,
                theme.description,
                theme.consolidated_insight,
                json.dumps(theme.session_ids, ensure_ascii=False),
                theme.created_at,
                theme.updated_at,
            ),
        )
        return theme

    def list_themes(self) -> list[CoreTheme]:
        rows = self._query(
            "SELECT * FROM tenant_core_themes WHERE user_id = ? ORDER BY updated_at DESC",
            (self._uid_param(),),
        )
        return [
            CoreTheme(
                id=r["id"],
                name=r["name"],
                description=r.get("description") or "",
                consolidated_insight=r.get("consolidated_insight") or "",
                session_ids=json.loads(r.get("session_ids_json") or "[]"),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def get_theme(self, theme_id: str) -> Optional[CoreTheme]:
        rows = self._query(
            "SELECT * FROM tenant_core_themes WHERE id = ? AND user_id = ?",
            (theme_id, self._uid_param()),
        )
        if not rows:
            return None
        r = rows[0]
        return CoreTheme(
            id=r["id"],
            name=r["name"],
            description=r.get("description") or "",
            consolidated_insight=r.get("consolidated_insight") or "",
            session_ids=json.loads(r.get("session_ids_json") or "[]"),
            created_at=r["created_at"],
            updated_at=r["updated_at"],
        )

    def clear_themes(self) -> None:
        self._query("DELETE FROM tenant_core_themes WHERE user_id = ?", (self._uid_param(),))
        self._query("UPDATE tenant_topic_sessions SET theme_id = NULL WHERE user_id = ?", (self._uid_param(),))

    # ------------------------------------------------------------------
    # Hybrid Search (BM25-like Lexical + Embedding Cosine)
    # ------------------------------------------------------------------
    def search_sessions(
        self,
        query: str,
        query_embedding: Optional[list[float]] = None,
        top_k: int = 5,
        exclude_session_id: Optional[str] = None,
    ) -> list[tuple[TopicSession, float]]:
        """根据自然语言描述在当前用户名下检索历史 TopicSession."""
        sessions = self.list_sessions(include_merged=False)
        if exclude_session_id:
            sessions = [s for s in sessions if s.id != exclude_session_id]
        if not sessions:
            return []

        q_tokens = tokenize(query)
        q_counter = Counter(q_tokens)

        scored: list[tuple[TopicSession, float]] = []
        for sess in sessions:
            doc_text = " ".join(
                [
                    sess.title * 3,
                    " ".join(sess.tags) * 2,
                    sess.summary * 2,
                    " ".join(sess.key_decisions),
                ]
            )
            doc_tokens = tokenize(doc_text)
            if not doc_tokens:
                lexical_score = 0.0
            else:
                doc_counter = Counter(doc_tokens)
                overlap = sum(
                    min(count, doc_counter[tok]) for tok, count in q_counter.items()
                )
                lexical_score = overlap / (
                    math.sqrt(len(q_tokens) + 1) * math.log1p(len(doc_tokens))
                )

            vec_score = 0.0
            if query_embedding and sess.embedding:
                vec_score = max(0.0, cosine_similarity(query_embedding, sess.embedding))

            total_score = lexical_score * 2.5 + vec_score
            scored.append((sess, total_score))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]
