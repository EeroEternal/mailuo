"""SQLite + Vector/Keyword Hybrid Storage for Topic Sessions and Core Themes."""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Optional

from .models import CoreTheme, MessageRecord, SessionStatus, TopicSession, utc_now_iso


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


class MailuoStorage:
    """持久化存储层：管理 TopicSession、MessageRecord 与 CoreTheme."""

    def __init__(self, db_path: str | Path = "~/.mailuo/mailuo.db") -> None:
        self.db_path = Path(db_path).expanduser().resolve()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript(
            """
            CREATE TABLE IF NOT EXISTS core_themes (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                consolidated_insight TEXT NOT NULL DEFAULT '',
                session_ids_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS topic_sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                tags_json TEXT NOT NULL DEFAULT '[]',
                theme_id TEXT,
                parent_session_id TEXT,
                status TEXT NOT NULL DEFAULT 'active',
                summary TEXT NOT NULL DEFAULT '',
                key_decisions_json TEXT NOT NULL DEFAULT '[]',
                open_questions_json TEXT NOT NULL DEFAULT '[]',
                embedding_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                is_ephemeral INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES topic_sessions(id)
            );

            CREATE TABLE IF NOT EXISTS app_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # Active Session Pointer
    # ------------------------------------------------------------------
    def get_active_session_id(self) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM app_state WHERE key = 'active_session_id'"
        ).fetchone()
        return row["value"] if row and row["value"] else None

    def set_active_session_id(self, session_id: Optional[str]) -> None:
        val = session_id or ""
        self._conn.execute(
            """
            INSERT INTO app_state (key, value) VALUES ('active_session_id', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (val,),
        )
        self._conn.commit()

    # ------------------------------------------------------------------
    # TopicSession CRUD
    # ------------------------------------------------------------------
    def save_session(self, session: TopicSession) -> TopicSession:
        session.updated_at = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO topic_sessions (
                id, title, tags_json, theme_id, parent_session_id, status,
                summary, key_decisions_json, open_questions_json, embedding_json,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                title = excluded.title,
                tags_json = excluded.tags_json,
                theme_id = excluded.theme_id,
                parent_session_id = excluded.parent_session_id,
                status = excluded.status,
                summary = excluded.summary,
                key_decisions_json = excluded.key_decisions_json,
                open_questions_json = excluded.open_questions_json,
                embedding_json = COALESCE(excluded.embedding_json, topic_sessions.embedding_json),
                updated_at = excluded.updated_at
            """,
            (
                session.id,
                session.title,
                json.dumps(session.tags, ensure_ascii=False),
                session.theme_id,
                session.parent_session_id,
                session.status.value,
                session.summary,
                json.dumps(session.key_decisions, ensure_ascii=False),
                json.dumps(session.open_questions, ensure_ascii=False),
                json.dumps(session.embedding) if session.embedding else None,
                session.created_at,
                session.updated_at,
            ),
        )
        self._conn.commit()
        return session

    def get_session(self, session_id: str) -> Optional[TopicSession]:
        row = self._conn.execute(
            "SELECT * FROM topic_sessions WHERE id = ?", (session_id,)
        ).fetchone()
        return self._row_to_session(row) if row else None

    def list_sessions(
        self, include_merged: bool = False, theme_id: Optional[str] = None
    ) -> list[TopicSession]:
        query = "SELECT * FROM topic_sessions WHERE 1=1"
        params: list[str] = []
        if not include_merged:
            query += " AND status != ?"
            params.append(SessionStatus.MERGED.value)
        if theme_id is not None:
            query += " AND theme_id = ?"
            params.append(theme_id)
        query += " ORDER BY updated_at DESC"
        rows = self._conn.execute(query, params).fetchall()
        return [self._row_to_session(r) for r in rows]

    def _row_to_session(self, row: sqlite3.Row) -> TopicSession:
        emb_raw = row["embedding_json"]
        return TopicSession(
            id=row["id"],
            title=row["title"],
            tags=json.loads(row["tags_json"] or "[]"),
            theme_id=row["theme_id"],
            parent_session_id=row["parent_session_id"],
            status=SessionStatus(row["status"]),
            summary=row["summary"] or "",
            key_decisions=json.loads(row["key_decisions_json"] or "[]"),
            open_questions=json.loads(row["open_questions_json"] or "[]"),
            embedding=json.loads(emb_raw) if emb_raw else None,
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # ------------------------------------------------------------------
    # Message CRUD
    # ------------------------------------------------------------------
    def add_message(self, msg: MessageRecord) -> MessageRecord:
        self._conn.execute(
            """
            INSERT INTO messages (id, session_id, role, content, is_ephemeral, metadata_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                msg.id,
                msg.session_id,
                msg.role,
                msg.content,
                1 if msg.is_ephemeral else 0,
                json.dumps(msg.metadata, ensure_ascii=False),
                msg.created_at,
            ),
        )
        # Touch session updated_at if it's not an ephemeral tangent
        if not msg.is_ephemeral:
            self._conn.execute(
                "UPDATE topic_sessions SET updated_at = ? WHERE id = ?",
                (utc_now_iso(), msg.session_id),
            )
        self._conn.commit()
        return msg

    def get_session_messages(
        self,
        session_id: str,
        include_ephemeral: bool = False,
        limit: Optional[int] = None,
    ) -> list[MessageRecord]:
        query = "SELECT * FROM messages WHERE session_id = ?"
        params: list[object] = [session_id]
        if not include_ephemeral:
            query += " AND is_ephemeral = 0"
        query += " ORDER BY created_at ASC"
        rows = self._conn.execute(query, params).fetchall()
        msgs = [
            MessageRecord(
                id=r["id"],
                session_id=r["session_id"],
                role=r["role"],
                content=r["content"],
                is_ephemeral=bool(r["is_ephemeral"]),
                metadata=json.loads(r["metadata_json"] or "{}"),
                created_at=r["created_at"],
            )
            for r in rows
        ]
        if limit and len(msgs) > limit:
            return msgs[-limit:]
        return msgs

    # ------------------------------------------------------------------
    # CoreTheme CRUD
    # ------------------------------------------------------------------
    def save_theme(self, theme: CoreTheme) -> CoreTheme:
        theme.updated_at = utc_now_iso()
        self._conn.execute(
            """
            INSERT INTO core_themes (
                id, name, description, consolidated_insight, session_ids_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                description = excluded.description,
                consolidated_insight = excluded.consolidated_insight,
                session_ids_json = excluded.session_ids_json,
                updated_at = excluded.updated_at
            """,
            (
                theme.id,
                theme.name,
                theme.description,
                theme.consolidated_insight,
                json.dumps(theme.session_ids, ensure_ascii=False),
                theme.created_at,
                theme.updated_at,
            ),
        )
        self._conn.commit()
        return theme

    def list_themes(self) -> list[CoreTheme]:
        rows = self._conn.execute(
            "SELECT * FROM core_themes ORDER BY updated_at DESC"
        ).fetchall()
        return [
            CoreTheme(
                id=r["id"],
                name=r["name"],
                description=r["description"],
                consolidated_insight=r["consolidated_insight"],
                session_ids=json.loads(r["session_ids_json"] or "[]"),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def get_theme(self, theme_id: str) -> Optional[CoreTheme]:
        row = self._conn.execute(
            "SELECT * FROM core_themes WHERE id = ?", (theme_id,)
        ).fetchone()
        if not row:
            return None
        return CoreTheme(
            id=row["id"],
            name=row["name"],
            description=row["description"],
            consolidated_insight=row["consolidated_insight"],
            session_ids=json.loads(row["session_ids_json"] or "[]"),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def clear_themes(self) -> None:
        self._conn.execute("DELETE FROM core_themes")
        self._conn.execute("UPDATE topic_sessions SET theme_id = NULL")
        self._conn.commit()

    # ------------------------------------------------------------------
    # Hybrid Search (BM25-like Lexical + Optional Embedding Cosine)
    # ------------------------------------------------------------------
    def search_sessions(
        self,
        query: str,
        query_embedding: Optional[list[float]] = None,
        top_k: int = 5,
        exclude_session_id: Optional[str] = None,
    ) -> list[tuple[TopicSession, float]]:
        """根据自然语言描述检索历史 TopicSession（支持『把上次讨论 XX 的话题调出来』）."""
        sessions = self.list_sessions(include_merged=False)
        if exclude_session_id:
            sessions = [s for s in sessions if s.id != exclude_session_id]
        if not sessions:
            return []

        q_tokens = tokenize(query)
        q_counter = Counter(q_tokens)

        scored: list[tuple[TopicSession, float]] = []
        for sess in sessions:
            # 拼接该 Session 的标题、标签、摘要、结论以及消息内容用于检索
            msgs = self.get_session_messages(sess.id, include_ephemeral=False, limit=10)
            msg_text = " ".join(m.content for m in msgs)
            doc_text = " ".join(
                [
                    sess.title * 3,  # 标题权重加倍
                    " ".join(sess.tags) * 2,
                    sess.summary * 2,
                    " ".join(sess.key_decisions),
                    msg_text,
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
                lexical_score = overlap / (math.sqrt(len(q_tokens) + 1) * math.log1p(len(doc_tokens)))

            vec_score = 0.0
            if query_embedding and sess.embedding:
                vec_score = max(0.0, cosine_similarity(query_embedding, sess.embedding))

            total_score = lexical_score * 2.5 + vec_score
            scored.append((sess, total_score))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]
