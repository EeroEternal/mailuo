"""Unit and integration tests for Mailuo (脉络) Topic-Centric Session Engine & Multi-Agent Bridges."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from mailuo.adapters import GoogleAgyBridge, PiDevBridge, run_interactive
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


def test_pi_and_agy_bridges_command_and_transcript_sync(tmp_path: Path) -> None:
    """验证拉起 pi.dev (`pi`) 与 Google Antigravity (`agy`) 的命令生成（含穿插隔离 `--no-session`）以及 JSONL 历史会话导入."""
    pi_dir = tmp_path / "pi_sessions" / "proj"
    pi_dir.mkdir(parents=True)
    pi_file = pi_dir / "2026-10-18_01a09e80-b7ef-7244-97b1-97596872c2b2.jsonl"
    pi_lines = [
        {"type": "session", "version": 3, "id": "01a09e80-b7ef-7244-97b1-97596872c2b2", "cwd": "/tmp"},
        {
            "type": "message",
            "id": "m1",
            "timestamp": "2026-10-18T10:00:00Z",
            "message": {
                "role": "user",
                "content": [{"type": "text", "text": "smartgate 在针对推理引擎和 agent 之间做了什么样的上下文处理"}],
            },
        },
        {
            "type": "message",
            "id": "m2",
            "timestamp": "2026-10-18T10:00:05Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "SmartGate 通过网关层对推理引擎与 Agent 上下文进行了分片缓存与路由优化。"}],
            },
        },
    ]
    pi_file.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in pi_lines), encoding="utf-8")

    agy_dir = tmp_path / "agy_brain" / "conv-agy-999" / ".system_generated" / "logs"
    agy_dir.mkdir(parents=True)
    agy_file = agy_dir / "transcript.jsonl"
    agy_lines = [
        {
            "step_index": 0,
            "type": "USER_INPUT",
            "created_at": "2026-10-18T11:00:00Z",
            "content": "<USER_REQUEST>\n设计一套基于 SQLite 的跨 Agent 脉络管理系统\n</USER_REQUEST>",
        },
        {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-18T11:00:10Z",
            "content": "我们可以使用 TopicSession 与 CoreTheme 两层模型来管理。",
        },
    ]
    agy_file.write_text("\n".join(json.dumps(x, ensure_ascii=False) for x in agy_lines), encoding="utf-8")

    pi_bridge = PiDevBridge(sessions_root=tmp_path / "pi_sessions")
    agy_bridge = GoogleAgyBridge(brain_root=tmp_path / "agy_brain")
    engine = MailuoEngine(
        db_path=tmp_path / "multi_agent.db",
        pi_bridge=pi_bridge,
        agy_bridge=agy_bridge,
    )

    # 1. 测试从磁盘同步 pi 和 agy 的原生会话
    imported = engine.sync_external_sessions(source="all")
    assert len(imported) == 2

    # 2. 测试用自然语言调出刚刚从 pi.dev 导入的关于 smartgate 推理引擎的话题
    recalled = engine.chat("上次我们讨论的那个 smartgate 推理引擎上下文处理的话题调出来")
    assert recalled.decision.action == RouteActionType.SWITCH_EXISTING
    assert recalled.active_session is not None
    assert recalled.active_session.external_sessions.get("pi") == "01a09e80-b7ef-7244-97b1-97596872c2b2"

    # 3. 验证正常推进时拉起 pi 会自动带上绑定的 --session-id
    normal_cmd = pi_bridge.build_command(
        recalled.active_session,
        prompt="继续深入聊聊缓存淘汰策略",
        is_tangent=False,
    )
    assert "--session-id" in normal_cmd
    assert "01a09e80-b7ef-7244-97b1-97596872c2b2" in normal_cmd
    assert "--no-session" not in normal_cmd

    # 4. 验证穿插提问时拉起 pi 会自动使用 --no-session 隔离，不污染 pi 原生会话
    tangent_cmd = pi_bridge.build_command(
        recalled.active_session,
        prompt="顺便问一下 bash 怎么查看端口占用",
        is_tangent=True,
    )
    assert "--no-session" in tangent_cmd
    assert "--session-id" not in tangent_cmd

    # 5. 验证同一话题切换到 Google agy 时，会自动注入之前在 pi 里聊出的摘要上下文
    agy_cmd = agy_bridge.build_command(
        recalled.active_session,
        prompt="请帮我把刚才在 pi 里讨论的网关缓存逻辑写成代码",
        is_tangent=False,
    )
    assert any("[Mailuo topic context]" in arg for arg in agy_cmd)

    # 6. 验证全新话题下直接运行 `mai agy` (interactive=True, prompt=None) 时，纯净启动 `agy`，绝不发送『Google agy 专题讨论』或『拉起 agy 交互式会话』
    from mailuo.models import TopicSession

    empty_sess = TopicSession(title="Unnamed topic")
    clean_agy_cmd = agy_bridge.build_command(
        empty_sess,
        prompt=None,
        interactive=True,
    )
    assert clean_agy_cmd == [agy_bridge.agy_bin]
    assert "-i" not in clean_agy_cmd

    # 7. 验证 `mai pi --session <id>`：用户自选会话时不再重复注入 --session-id，且同步该会话的记录
    resume_cmd = pi_bridge.build_command(
        recalled.active_session,
        prompt=None,
        interactive=True,
        extra_args=["--session", "01a09e80-b7ef"],
    )
    assert resume_cmd[1:3] == ["--session", "01a09e80-b7ef"]
    assert "--session-id" not in resume_cmd



def test_run_interactive_returns_child_exit_code() -> None:
    """Ctrl+C 由子 Agent 自行处理：mai 只等待子进程退出，不能自己先死掉."""
    proc = run_interactive([sys.executable, "-c", "import sys; sys.exit(3)"])
    assert proc.returncode == 3


def test_multi_user_tenant_isolation(tmp_path: Path) -> None:
    """验证多用户注册后，不同用户的数据在同一数据库中严格按 user_id 隔离，绝不串号."""
    shared_db = tmp_path / "shared_tenant.db"
    engine_alice = MailuoEngine(db_path=shared_db, user_id="usr_alice")
    engine_bob = MailuoEngine(db_path=shared_db, user_id="usr_bob")

    # Alice 创建自己的话题
    res_alice = engine_alice.chat("我是 Alice，我在研究基于 Rust 的高频交易引擎。")
    assert res_alice.active_session is not None
    alice_sess_id = res_alice.active_session.id

    # Bob 创建自己的话题
    res_bob = engine_bob.chat("我是 Bob，我在设计 Kubernetes 集群自动扩缩容方案。")
    assert res_bob.active_session is not None
    bob_sess_id = res_bob.active_session.id

    # 验证 Alice 只能看到自己的 Session，看不到 Bob 的
    alice_sessions = engine_alice.storage.list_sessions()
    assert [s.id for s in alice_sessions] == [alice_sess_id]
    assert engine_alice.storage.get_session(bob_sess_id) is None

    # 验证 Bob 只能看到自己的 Session，看不到 Alice 的
    bob_sessions = engine_bob.storage.list_sessions()
    assert [s.id for s in bob_sessions] == [bob_sess_id]
    assert engine_bob.storage.get_session(alice_sess_id) is None



def test_run_interactive_survives_sigint(monkeypatch) -> None:
    """Ctrl+C 落在 poll()/wait() 之间时必须继续等子进程，而不是让 mai 先死掉."""
    import os
    import signal
    import threading

    def interrupt() -> None:
        os.kill(os.getpid(), signal.SIGINT)

    timer = threading.Timer(0.2, interrupt)
    timer.start()
    try:
        proc = run_interactive([sys.executable, "-c", "import time; time.sleep(0.6)"])
    finally:
        timer.cancel()
    assert proc.returncode == 0


def test_sigint_during_sync_is_ignored(tmp_path: Path, monkeypatch) -> None:
    """pi 退出后的同步阶段再按 Ctrl+C，也不能丢掉已经捕获的对话."""
    import os
    import signal

    from mailuo.adapters import AgentExecutionResult

    engine = MailuoEngine(db_path=tmp_path / "sigint.db")

    def fake_run_turn(**kwargs) -> AgentExecutionResult:
        return AgentExecutionResult(
            agent_name="pi",
            external_session_id="fake",
            command=["pi"],
            reply="done",
            returncode=0,
            captured_messages=[
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "world"},
            ],
        )

    monkeypatch.setattr(engine.pi_bridge, "run_turn", fake_run_turn)

    real_add_message = engine.storage.add_message
    fired = {"done": False}

    def add_message_with_sigint(record):
        if not fired["done"]:
            fired["done"] = True
            os.kill(os.getpid(), signal.SIGINT)
        return real_add_message(record)

    monkeypatch.setattr(engine.storage, "add_message", add_message_with_sigint)

    sess = engine.launch_agent_interactive("pi")
    assert sess is not None
    assert fired["done"]
    contents = [m.content for m in engine.storage.get_session_messages(sess.id)]
    assert contents == ["hello", "world"]
