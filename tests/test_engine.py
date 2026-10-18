"""Unit and integration tests for Mailuo (脉络) Topic-Centric Session Engine."""

from __future__ import annotations

from pathlib import Path

from mailuo.engine import MailuoEngine
from mailuo.models import RouteActionType


def test_topic_creation_and_side_tangent_isolation(tmp_path: Path) -> None:
    """验证在某个具体话题下穿插其他事情时，会自动隔离而不污染当前话题的上下文与摘要."""
    db_file = tmp_path / "test.db"
    engine = MailuoEngine(db_path=db_file)

    # 1. 开启具体话题 A
    r1 = engine.chat("我想讨论一下采用 SQLite 和向量检索来做知识库架构的设计。")
    assert r1.decision.action == RouteActionType.NEW_TOPIC
    assert r1.active_session is not None
    sess_a_id = r1.active_session.id

    # 2. 继续深入讨论话题 A
    r2 = engine.chat("我们决定把核心表分为 topic_sessions 和 messages 两张表。")
    assert r2.decision.action == RouteActionType.CONTINUE
    assert r2.active_session.id == sess_a_id

    # 3. 穿插一个完全无关的问题（测试 Side-Tangent 隔离）
    r3 = engine.chat("顺便问一下，帮我写个匹配邮箱的正则表达式。")
    assert r3.decision.action == RouteActionType.SIDE_TANGENT
    assert r3.active_session.id == sess_a_id

    # 验证主话题的非临时消息列表不包含穿插提问，且摘要未被“正则表达式”污染
    clean_msgs = engine.storage.get_session_messages(sess_a_id, include_ephemeral=False)
    assert len(clean_msgs) == 4  # 2轮正常对话 (2 user + 2 assistant)
    assert all("正则表达式" not in m.content for m in clean_msgs)

    updated_sess_a = engine.storage.get_session(sess_a_id)
    assert updated_sess_a is not None
    assert "正则表达式" not in updated_sess_a.summary


def test_recall_previous_sessions_and_consolidate_themes(tmp_path: Path) -> None:
    """验证『把上次讨论的那个话题的 session 调出来』以及『整理成跟我有关的最核心主题』."""
    db_file = tmp_path / "test_recall.db"
    engine = MailuoEngine(db_path=db_file)

    # 话题 1：SQLite 向量数据库
    t1 = engine.chat("我们来聊聊 SQLite 向量数据库的存储引擎选型方案。")
    sess1_id = t1.active_session.id

    # 话题 2：Agent Session 自动路由管理
    t2 = engine.chat("我们换个话题：设计一套针对具体内容的 Agent Session 自动路由与防污染机制。")
    sess2_id = t2.active_session.id
    assert sess2_id != sess1_id

    # 话题 3：个人量化交易策略
    t3 = engine.chat("新开一个话题：构建个人股票量化交易双均线回测系统。")
    sess3_id = t3.active_session.id
    assert sess3_id not in (sess1_id, sess2_id)

    # 自然语言召回话题 1
    recall_1 = engine.chat("上次我们讨论的那个 SQLite 向量数据库的话题的 session 你可以调出来吗？")
    assert recall_1.decision.action == RouteActionType.SWITCH_EXISTING
    assert recall_1.active_session.id == sess1_id

    # 自然语言召回话题 2
    recall_2 = engine.chat("上次我们讨论的另外一个 Agent Session 自动路由的话题你是不是可以调出来？")
    assert recall_2.decision.action == RouteActionType.SWITCH_EXISTING
    assert recall_2.active_session.id == sess2_id

    # 自然语言触发核心主题整理
    consolidate_res = engine.chat("把这些 session 整理成一些跟我有关的最核心主题的 session。")
    assert consolidate_res.decision.action == RouteActionType.CONSOLIDATE
    assert consolidate_res.consolidated_themes is not None
    assert len(consolidate_res.consolidated_themes) >= 2

    # 验证所有 Session 均已挂载到对应的 CoreTheme 下
    all_sessions = engine.storage.list_sessions()
    assert all(s.theme_id is not None for s in all_sessions)
