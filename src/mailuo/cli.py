"""Command-line interface and interactive walkthrough for Mailuo (脉络)."""

from __future__ import annotations

import argparse
import tempfile
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .engine import MailuoEngine, TurnResult
from .models import RouteActionType

console = Console()


def render_turn(user_text: str, result: TurnResult) -> None:
    console.print(f"\n[bold cyan]🧑 用户[/bold cyan]: {user_text}")

    action_badge = {
        RouteActionType.NEW_TOPIC: "[bold green]🌱 新建话题 Session[/bold green]",
        RouteActionType.CONTINUE: "[bold blue]🎯 延续当前话题[/bold blue]",
        RouteActionType.SIDE_TANGENT: "[bold yellow]🛡️ 临时穿插隔离 (Side-Tangent)[/bold yellow]",
        RouteActionType.SWITCH_EXISTING: "[bold magenta]🔍 语义召回历史 Session[/bold magenta]",
        RouteActionType.CONSOLIDATE: "[bold bright_cyan]🧠 核心主题归纳整理[/bold bright_cyan]",
    }.get(result.decision.action, str(result.decision.action))

    sess_title = (
        f"《{result.active_session.title}》 ({result.active_session.id})"
        if result.active_session
        else "全局视图"
    )
    console.print(
        f"  [dim]└─ 脉络路由:[/dim] {action_badge} | [bold]{sess_title}[/bold]\n"
        f"  [dim]└─ 路由依据:[/dim] [italic]{result.decision.reason}[/italic]"
    )
    console.print(
        Panel(
            Markdown(result.reply),
            title="[bold green]🤖 Agent 响应[/bold green]",
            border_style="green" if result.decision.action != RouteActionType.SIDE_TANGENT else "yellow",
        )
    )


def print_sessions_table(engine: MailuoEngine) -> None:
    sessions = engine.storage.list_sessions(include_merged=False)
    active_id = engine.storage.get_active_session_id()
    table = Table(title="📂 当前具体话题 Sessions 列表 (Topic Sessions)", show_lines=True)
    table.add_column("状态", width=8)
    table.add_column("Session ID", style="cyan", no_wrap=True)
    table.add_column("具体话题标题", style="bold")
    table.add_column("标签", style="magenta")
    table.add_column("所属核心主题", style="green")
    table.add_column("结构化脉络摘要 (不受穿插问题污染)")

    for s in sessions:
        marker = "🟢 激活" if s.id == active_id else "⏸️ 挂起"
        theme_name = "-"
        if s.theme_id:
            th = engine.storage.get_theme(s.theme_id)
            if th:
                theme_name = th.name
        table.add_row(
            marker,
            s.id,
            s.title,
            ", ".join(s.tags),
            theme_name,
            s.summary or "(暂无摘要)",
        )
    console.print(table)


def run_demo() -> None:
    """完整演示用户提出的三大核心诉求：
    1. 围绕具体内容形成独立 Session，穿插其他问题时自动隔离不搞乱上下文
    2. 自然语言『把上次我们讨论的那个 XX 话题的 session 调出来』
    3. 自动把零散的 session 整理成跟用户有关的最核心主题 (Core Themes)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "demo_mailuo.db"
        engine = MailuoEngine(db_path=db_file)

        console.print(
            Panel.fit(
                "[bold bright_white]Mailuo (脉络) 核心引擎演示[/bold bright_white]\n"
                "演示场景：多话题探讨 -> 穿插杂项自动隔离 -> 切换到新话题 -> 语义调出历史话题 -> 自动整理核心主题",
                border_style="bright_blue",
            )
        )

        script = [
            # 话题 1：探讨数据库与向量检索架构
            "我想讨论一下我们的知识库后端架构，准备采用 SQLite 配合向量检索来存对话数据。",
            "我们决定核心表拆成 topic_sessions 和 messages 两张表，查询性能要控制在 10ms 以内。",
            # 穿插一个完全无关的问题（测试不会把当前话题搞杂乱）
            "顺便问一下，帮我写个匹配邮箱的 Python 正则表达式，别弄乱我们刚才聊的架构话题。",
            # 话题 2：开启另一个具体话题（Agent Session 路由机制）
            "我们换个话题：来探讨一下 Agent 的 Session 自动路由和上下文防污染机制怎么设计。",
            "核心方案是每次用户输入先经过一个轻量级 Router，判断是延续话题、临时穿插还是调出旧话题。",
            # 话题 3：开启第三个具体话题（个人量化交易策略）
            "新开一个话题：我想设计一个基于双均线和波动率过滤的个人量化交易回测系统。",
            # 核心诉求 1：自然语言调出上次讨论的特定话题 Session
            "上次我们讨论的那个 SQLite 向量检索后端架构的话题的 session 你可以调出来吗？",
            # 核心诉求 2：自然语言调出另一个历史话题 Session
            "上次我们讨论的另外一个关于 Agent Session 自动路由的话题你是不是也可以调出来？",
            # 核心诉求 3：把零散的 Session 整理成最核心主题
            "请把这些 session 整理成跟我有关的最核心主题的 session。",
        ]

        for user_input in script:
            res = engine.chat(user_input)
            render_turn(user_input, res)

        console.print("\n")
        print_sessions_table(engine)


def run_repl(db_path: str) -> None:
    engine = MailuoEngine(db_path=db_path)
    console.print(
        Panel.fit(
            "[bold cyan]Mailuo (脉络) - 交互式命令行[/bold cyan]\n"
            "直接输入自然语言即可对话（支持自动识别『顺便问一下...』、『把上次讨论XX的session调出来』、『整理核心主题』）。\n"
            "快捷指令：\n"
            "  [bold]/list[/bold]        查看所有具体话题 Sessions\n"
            "  [bold]/themes[/bold]      触发整理并查看核心主题 (Core Themes)\n"
            "  [bold]/new <文本>[/bold]  强制开启新话题\n"
            "  [bold]/btw <文本>[/bold]  强制以隔离沙箱提问（不污染当前话题）\n"
            "  [bold]/recall <词>[/bold] 检索并调出历史话题\n"
            "  [bold]/quit[/bold]        退出",
            border_style="cyan",
        )
    )

    while True:
        active = engine.get_active_session()
        prompt_prefix = f"[{active.title}] > " if active else "[无激活话题] > "
        try:
            text = console.input(f"\n[bold green]{prompt_prefix}[/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n再见！")
            break

        if not text:
            continue
        if text in ("/quit", "/exit", "exit", "quit"):
            break
        if text == "/list":
            print_sessions_table(engine)
            continue
        if text == "/themes":
            res = engine.chat("请把这些 session 整理成跟我有关的最核心主题")
            render_turn("/themes", res)
            print_sessions_table(engine)
            continue
        if text.startswith("/new "):
            res = engine.chat(text[5:].strip(), force_new_topic=True)
            render_turn(text, res)
            continue
        if text.startswith("/btw "):
            res = engine.chat(text[5:].strip(), force_tangent=True)
            render_turn(text, res)
            continue
        if text.startswith("/recall "):
            q = text[8:].strip()
            recalled = engine.recall_by_query(q)
            if recalled:
                console.print(f"[bold magenta]已调出并切换至话题：[/bold magenta]《{recalled.title}》 ({recalled.id})")
                console.print(f"[dim]摘要：{recalled.summary}[/dim]")
            else:
                console.print("[red]未找到匹配的历史话题 Session[/red]")
            continue

        res = engine.chat(text)
        render_turn(text, res)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mailuo (脉络): 针对具体内容与核心主题的 Agent Session 管理系统"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="运行完整场景演示（穿插隔离、自然语言调出历史话题、核心主题聚类整理）",
    )
    parser.add_argument(
        "--db",
        default="~/.mailuo/mailuo.db",
        help="SQLite 数据库存储路径 (默认: ~/.mailuo/mailuo.db)",
    )
    args = parser.parse_args()

    if args.demo:
        run_demo()
    else:
        run_repl(args.db)


if __name__ == "__main__":
    main()
