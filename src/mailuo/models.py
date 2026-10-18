"""Data models for Topic Sessions, Core Themes, and Messages."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def short_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


class SessionStatus(str, Enum):
    ACTIVE = "active"
    PAUSED = "paused"
    MERGED = "merged"
    ARCHIVED = "archived"


class RouteActionType(str, Enum):
    CONTINUE = "continue"          # 延续当前话题
    SIDE_TANGENT = "side_tangent"  # 穿插临时问题（隔离上下文，不污染当前主话题）
    SWITCH_EXISTING = "switch"     # 召回并切换到已有的历史话题 Session
    NEW_TOPIC = "new_topic"        # 开启一个全新的具体话题 Session
    CONSOLIDATE = "consolidate"    # 触发整理核心主题


class CoreTheme(BaseModel):
    """用户长期的『核心主题』（由多个具体 TopicSession 归纳整理而成）."""

    id: str = Field(default_factory=lambda: short_id("theme"))
    name: str = Field(..., description="核心主题名称，例如：Agent架构设计、个人量化交易系统")
    description: str = Field(default="", description="核心主题的定位与范围描述")
    consolidated_insight: str = Field(
        default="",
        description="跨多个 Session 提炼出的核心共识、长期原则与关键结论",
    )
    session_ids: list[str] = Field(default_factory=list, description="关联的具体 TopicSession ID 列表")
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class TopicSession(BaseModel):
    """针对某个『具体内容/话题』的独立上下文 Session."""

    id: str = Field(default_factory=lambda: short_id("sess"))
    title: str = Field(..., description="具体讨论的话题标题")
    tags: list[str] = Field(default_factory=list, description="话题关键词标签")
    theme_id: Optional[str] = Field(default=None, description="所属的核心主题 ID (CoreTheme.id)")
    parent_session_id: Optional[str] = Field(
        default=None, description="如果是从某个话题分流/派生的，记录父 Session ID"
    )
    status: SessionStatus = Field(default=SessionStatus.ACTIVE)

    # 结构化状态摘要：避免每次加载全量冗长历史，同时保证随时召回时上下文清晰
    summary: str = Field(default="", description="当前话题的核心进展摘要")
    key_decisions: list[str] = Field(default_factory=list, description="已达成的关键结论或决策")
    open_questions: list[str] = Field(default_factory=list, description="待解决的问题或下一步计划")

    # 跨 Agent 外部会话绑定（例如 {"pi": "uuid-1234", "agy": "conv-5678"}）
    external_sessions: dict[str, str] = Field(
        default_factory=dict,
        description="绑定的外部 Agent 原生 Session ID 映射 (如 pi, agy)",
    )

    embedding: Optional[list[float]] = Field(default=None, exclude=True)
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class MessageRecord(BaseModel):
    """单条对话消息记录，绑定到具体的 TopicSession 或标记为临时穿插消息."""

    id: str = Field(default_factory=lambda: short_id("msg"))
    session_id: str = Field(..., description="所属的 TopicSession ID")
    role: str = Field(..., description="user / assistant / system")
    content: str = Field(..., description="消息内容")
    agent_source: str = Field(
        default="mailuo",
        description="产生或处理该消息的 Agent 来源 (如 pi, agy, mailuo)",
    )
    is_ephemeral: bool = Field(
        default=False,
        description="是否为临时穿插提问（Side-chat）。若为 True，则不会污染主 Session 的长期上下文",
    )
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)


class RouteDecision(BaseModel):
    """路由器对用户最新输入的意图与脉络判定结果."""

    action: RouteActionType
    target_session_id: Optional[str] = Field(
        default=None, description="当 action 为 switch 或 continue 时的目标 Session ID"
    )
    new_topic_title: Optional[str] = Field(
        default=None, description="当 action 为 new_topic 时建议的新话题标题"
    )
    new_topic_tags: list[str] = Field(default_factory=list)
    search_query: Optional[str] = Field(
        default=None, description="当用户想召回历史话题时提取的检索词"
    )
    reason: str = Field(default="", description="为什么做出该路由决定的简短解释")
