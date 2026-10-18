"""Simplified Command-Line Interface for Mailuo (脉络 `mai` — Cloudflare D1 云端存储 + 极简 CLI).

极简命令用法：
  mai                     # 进入交互式对话（默认直连 Cloudflare D1）
  mai "一句话或问题"      # 直接对话（自动判断延续话题 / 临时穿插隔离 / 调出旧话题 / 整理核心主题）
  mai pi [提示词]         # 在当前话题脉络下拉起 pi.dev (退出后自动同步记录到 Cloudflare D1)
  mai agy [提示词]        # 在当前话题脉络下拉起 Google Antigravity (`agy`)
  mai ls                  # 查看所有话题 Sessions 列表
  mai themes              # 一键把所有零散 Session 整理为核心主题 (Core Themes)
  mai sync                # 一键把本机 pi.dev 和 agy 的历史记录同步到 Cloudflare D1
  mai demo                # 运行完整演示
"""

from __future__ import annotations

import os
import shlex
import sys
import tempfile
from pathlib import Path

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .engine import MailuoEngine, TurnResult
from .models import RouteActionType

console = Console()


def render_turn(user_text: str, result: TurnResult, storage_label: str = "") -> None:
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
    ext_bindings = ""
    if result.active_session and result.active_session.external_sessions:
        pairs = [f"{k}:{v[:8]}" for k, v in result.active_session.external_sessions.items()]
        ext_bindings = f" | 绑定: [cyan]{', '.join(pairs)}[/cyan]"

    db_note = f" | ☁️ [dim]{storage_label}[/dim]" if storage_label else ""
    console.print(
        f"  [dim]└─ 脉络路由:[/dim] {action_badge} | [bold]{sess_title}[/bold]{ext_bindings}{db_note}\n"
        f"  [dim]└─ 路由依据:[/dim] [italic]{result.decision.reason}[/italic]"
    )
    if result.agent_execution:
        cmd_preview = " ".join(shlex.quote(c) for c in result.agent_execution.command[:6])
        if len(result.agent_execution.command) > 6:
            cmd_preview += " ..."
        console.print(
            f"  [dim]└─ 外部 Agent ({result.agent_execution.agent_name}):[/dim] [green]{cmd_preview}[/green]"
        )

    console.print(
        Panel(
            Markdown(result.reply),
            title=f"[bold green]🤖 Agent ({result.agent_backend})[/bold green]",
            border_style="green"
            if result.decision.action != RouteActionType.SIDE_TANGENT
            else "yellow",
        )
    )


def print_sessions_table(engine: MailuoEngine) -> None:
    sessions = engine.storage.list_sessions(include_merged=False)
    active_id = engine.storage.get_active_session_id()
    table = Table(
        title=f"📂 话题 Sessions 列表 — 存储后端: {engine.storage.db_label}",
        show_lines=True,
    )
    table.add_column("状态", width=8)
    table.add_column("Session ID", style="cyan", no_wrap=True)
    table.add_column("具体话题标题", style="bold")
    table.add_column("绑定 Agent", style="bright_magenta")
    table.add_column("所属核心主题", style="green")
    table.add_column("结构化脉络摘要 (不受穿插问题污染)")

    for s in sessions:
        marker = "🟢 激活" if s.id == active_id else "⏸️ 挂起"
        theme_name = "-"
        if s.theme_id:
            th = engine.storage.get_theme(s.theme_id)
            if th:
                theme_name = th.name
        ext_str = (
            "\n".join(f"{k}: {v[:10]}.." for k, v in s.external_sessions.items())
            if s.external_sessions
            else "mailuo"
        )
        table.add_row(
            marker,
            s.id,
            s.title,
            ext_str,
            theme_name,
            s.summary or "(暂无摘要)",
        )
    console.print(table)


def run_demo() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "demo_mailuo.db"
        engine = MailuoEngine(db_path=db_file)

        console.print(
            Panel.fit(
                "[bold bright_white]Mailuo (脉络) 极简演示[/bold bright_white]\n"
                "支持 Cloudflare D1 云端存储 + `pi.dev` (`pi`) / `Google Antigravity` (`agy`) 托管",
                border_style="bright_blue",
            )
        )

        script = [
            "我想讨论一下我们的知识库后端架构，准备采用 Cloudflare D1 配合向量检索来存对话数据。",
            "我们决定核心表拆成 topic_sessions 和 messages 两张表。",
            "顺便问一下，帮我写个匹配邮箱的 Python 正则表达式，别弄乱我们刚才聊的架构话题。",
            "我们换个话题：来探讨一下 Agent 的 Session 自动路由和上下文防污染机制怎么设计。",
            "上次我们讨论的那个 Cloudflare D1 向量检索后端架构的话题的 session 你可以调出来吗？",
            "请把这些 session 整理成跟我有关的最核心主题的 session。",
        ]

        for user_input in script:
            res = engine.chat(user_input)
            render_turn(user_input, res, engine.storage.db_label)

        console.print("\n")
        print_sessions_table(engine)


def run_repl(engine: MailuoEngine) -> None:
    console.print(
        Panel.fit(
            f"[bold cyan]Mailuo (脉络)[/bold cyan] | 存储: [bold green]{engine.storage.db_label}[/bold green]\n"
            "直接输入任何话即可（支持自动识别『顺便问一下...』、『把上次讨论XX的话题调出来』、『整理核心主题』）。\n"
            "极简命令: [bold]ls[/bold] (话题列表) | [bold]themes[/bold] (整理核心主题) | [bold]pi[/bold] (拉起pi) | [bold]agy[/bold] (拉起agy) | [bold]sync[/bold] (同步历史) | [bold]q[/bold] (退出)",
            border_style="cyan",
        )
    )

    while True:
        active = engine.get_active_session()
        topic_label = active.title if active else "新话题"
        try:
            text = console.input(f"\n[bold green]({topic_label}) > [/bold green]").strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n再见！")
            break

        if not text:
            continue
        if text in ("q", "quit", "exit", "/quit"):
            break
        if text in ("ls", "/ls", "/list"):
            print_sessions_table(engine)
            continue
        if text in ("themes", "/themes"):
            res = engine.chat("请把这些 session 整理成跟我有关的最核心主题")
            render_turn("themes", res, engine.storage.db_label)
            print_sessions_table(engine)
            continue
        if text in ("sync", "/sync"):
            imported = engine.sync_external_sessions(source="all", limit_per_source=8)
            console.print(
                f"[bold green]☁️ 已同步 {len(imported)} 个本机 pi/agy 会话到 {engine.storage.db_label}！[/bold green]"
            )
            print_sessions_table(engine)
            continue
        if text == "pi" or text.startswith("pi "):
            prompt_part = text[3:].strip()
            if prompt_part:
                res = engine.chat(prompt_part, agent_backend="pi")
            else:
                if not active:
                    engine.create_topic_session(title="pi.dev 专题讨论", tags=["pi.dev"])
                res = engine.chat("进入 pi 交互式会话", agent_backend="pi", interactive_agent=True)
            render_turn(text, res, engine.storage.db_label)
            continue
        if text == "agy" or text.startswith("agy "):
            prompt_part = text[4:].strip()
            if prompt_part:
                res = engine.chat(prompt_part, agent_backend="agy")
            else:
                if not active:
                    engine.create_topic_session(title="Google agy 专题讨论", tags=["agy"])
                res = engine.chat("进入 agy 交互式会话", agent_backend="agy", interactive_agent=True)
            render_turn(text, res, engine.storage.db_label)
            continue

        res = engine.chat(text)
        render_turn(text, res, engine.storage.db_label)


HELP_TEXT = """[bold cyan]mai (脉络 Mailuo) — 极简 CLI (数据默认实时保存至 Cloudflare D1)[/bold cyan]

用法:
  [bold green]mai[/bold green]                       启动交互式对话
  [bold green]mai "<任意对话内容>"[/bold green]      直接发送一句话（自动路由/穿插隔离/调出历史话题/整理主题）
  [bold green]mai pi [提问内容][/bold green]         在当前脉络下拉起 [bold]pi.dev[/bold]（不带内容则进入 pi 交互式 TUI）
  [bold green]mai agy [提问内容][/bold green]        在当前脉络下拉起 [bold]Google agy[/bold]（不带内容则进入 agy 交互式界面）
  [bold green]mai ls[/bold green]                    查看 Cloudflare D1 中的所有具体话题 Sessions
  [bold green]mai themes[/bold green]                把所有零散 Session 自动整理成与你相关的『最核心主题』
  [bold green]mai sync[/bold green]                  将本机 pi.dev 和 agy 的历史记录一键同步至 Cloudflare D1
  [bold green]mai demo[/bold green]                  运行自动化演示
"""


def main() -> None:
    db_target = os.getenv("MAILUO_DB", "d1")
    args = sys.argv[1:]

    if not args:
        engine = MailuoEngine(db_path=db_target)
        run_repl(engine)
        return

    cmd = args[0]
    rest = " ".join(args[1:]).strip()

    if cmd in ("-h", "--help", "help"):
        console.print(Panel.fit(HELP_TEXT, border_style="cyan"))
        return

    if cmd in ("demo", "--demo"):
        run_demo()
        return

    engine = MailuoEngine(db_path=db_target)

    if cmd in ("ls", "list", "--list"):
        print_sessions_table(engine)
        return

    if cmd in ("themes", "theme", "--themes"):
        res = engine.chat("请把这些 session 整理成跟我有关的最核心主题")
        render_turn("mai themes", res, engine.storage.db_label)
        print_sessions_table(engine)
        return

    if cmd in ("sync", "--sync"):
        src = args[1] if len(args) > 1 else "all"
        imported = engine.sync_external_sessions(source=src, limit_per_source=10)
        console.print(
            f"[bold green]☁️ 已成功将 {len(imported)} 个本机 ({src}) 会话同步至 {engine.storage.db_label}！[/bold green]"
        )
        print_sessions_table(engine)
        return

    if cmd == "pi":
        if rest:
            res = engine.chat(rest, agent_backend="pi")
        else:
            if not engine.get_active_session():
                engine.create_topic_session(title="pi.dev 专题讨论", tags=["pi.dev"])
            res = engine.chat("拉起 pi 交互式会话", agent_backend="pi", interactive_agent=True)
        render_turn(f"mai pi {rest}".strip(), res, engine.storage.db_label)
        return

    if cmd == "agy":
        if rest:
            res = engine.chat(rest, agent_backend="agy")
        else:
            if not engine.get_active_session():
                engine.create_topic_session(title="Google agy 专题讨论", tags=["agy"])
            res = engine.chat("拉起 agy 交互式会话", agent_backend="agy", interactive_agent=True)
        render_turn(f"mai agy {rest}".strip(), res, engine.storage.db_label)
        return

    # 其他情况：直接把参数当做自然语言对话输入！
    full_input = " ".join(args).strip()
    res = engine.chat(full_input)
    render_turn(full_input, res, engine.storage.db_label)


if __name__ == "__main__":
    main()
