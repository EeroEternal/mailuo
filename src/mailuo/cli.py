"""Command-line interface and multi-agent orchestrator for Mailuo (脉络 — 支持 pi.dev 与 Google agy)."""

from __future__ import annotations

import argparse
import shlex
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
    ext_bindings = ""
    if result.active_session and result.active_session.external_sessions:
        pairs = [f"{k}:{v[:8]}" for k, v in result.active_session.external_sessions.items()]
        ext_bindings = f" | 外部绑定: [cyan]{', '.join(pairs)}[/cyan]"

    console.print(
        f"  [dim]└─ 脉络路由:[/dim] {action_badge} | [bold]{sess_title}[/bold]{ext_bindings}\n"
        f"  [dim]└─ 路由依据:[/dim] [italic]{result.decision.reason}[/italic]"
    )
    if result.agent_execution:
        cmd_preview = " ".join(shlex.quote(c) for c in result.agent_execution.command[:6])
        if len(result.agent_execution.command) > 6:
            cmd_preview += " ..."
        console.print(
            f"  [dim]└─ 拉起外部 Agent ({result.agent_execution.agent_name}):[/dim] [green]{cmd_preview}[/green]"
        )

    console.print(
        Panel(
            Markdown(result.reply),
            title=f"[bold green]🤖 Agent ({result.agent_backend}) 响应[/bold green]",
            border_style="green"
            if result.decision.action != RouteActionType.SIDE_TANGENT
            else "yellow",
        )
    )


def print_sessions_table(engine: MailuoEngine) -> None:
    sessions = engine.storage.list_sessions(include_merged=False)
    active_id = engine.storage.get_active_session_id()
    table = Table(
        title="📂 当前具体话题 Sessions 列表 (跨 pi.dev & Google agy 统一脉络)",
        show_lines=True,
    )
    table.add_column("状态", width=8)
    table.add_column("Session ID", style="cyan", no_wrap=True)
    table.add_column("具体话题标题", style="bold")
    table.add_column("绑定 Agent 会话", style="bright_magenta")
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
    """完整演示跨 Agent（pi.dev 与 Google agy）的脉络管理、穿插隔离、真实磁盘会话同步与核心主题整理."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "demo_mailuo.db"
        engine = MailuoEngine(db_path=db_file)

        console.print(
            Panel.fit(
                "[bold bright_white]Mailuo (脉络) × pi.dev (`pi`) × Google Antigravity (`agy`) 演示[/bold bright_white]\n"
                "1. 自动同步本机真实的 `~/.pi/agent/sessions` 与 `~/.gemini/antigravity-cli/brain` 历史会话\n"
                "2. 为每个具体话题生成专属 `pi --session-id` / `agy --conversation`，穿插提问自动加 `pi --no-session`\n"
                "3. 跨 Agent 自然语言调出历史话题，并自动聚类整理『核心主题』",
                border_style="bright_blue",
            )
        )

        # 第一步：演示直接从本机同步真实的 pi.dev 与 Google agy 历史会话
        imported = engine.sync_external_sessions(source="all", limit_per_source=2)
        if imported:
            console.print(
                f"\n[bold green]✅ 已从本机 `~/.pi/agent/sessions` 和 `~/.gemini/antigravity-cli/brain` 自动导入 {len(imported)} 个真实历史会话：[/bold green]"
            )
            for imp in imported:
                agents_str = ", ".join(f"{k}={v[:12]}..." for k, v in imp.external_sessions.items())
                console.print(f"  • [bold]《{imp.title}》[/bold] ({imp.id}) <- [{agents_str}]")

        # 第二步：演示多话题流转与拉起 pi / agy 的命令生成展示
        script = [
            "我想讨论一下我们的知识库后端架构，准备采用 SQLite 配合向量检索来存对话数据。",
            "我们决定核心表拆成 topic_sessions 和 messages 两张表，查询性能要控制在 10ms 以内。",
            "顺便问一下，帮我写个匹配邮箱的 Python 正则表达式，别弄乱我们刚才聊的架构话题。",
            "我们换个话题：来探讨一下 Agent 的 Session 自动路由和上下文防污染机制怎么设计。",
            "上次我们讨论的那个 SQLite 向量检索后端架构的话题的 session 你可以调出来吗？",
            "请把这些 session 整理成跟我有关的最核心主题的 session。",
        ]

        for user_input in script:
            res = engine.chat(user_input)
            render_turn(user_input, res)

            # 展示如果此时拉起 pi 或 agy，Mailuo 会如何自动绑定 session-id 或使用 --no-session 隔离
            if res.active_session and res.decision.action in (
                RouteActionType.CONTINUE,
                RouteActionType.SIDE_TANGENT,
                RouteActionType.SWITCH_EXISTING,
            ):
                is_tan = res.decision.action == RouteActionType.SIDE_TANGENT
                pi_cmd = engine.pi_bridge.build_command(
                    res.active_session,
                    prompt=user_input,
                    is_tangent=is_tan,
                    interactive=False,
                )
                agy_cmd = engine.agy_bridge.build_command(
                    res.active_session,
                    prompt=user_input,
                    is_tangent=is_tan,
                    interactive=False,
                )
                engine.storage.save_session(res.active_session)
                console.print(
                    f"  [dim]├─ 📌 若拉起 pi.dev，Mailuo 托管命令:[/dim] [bright_cyan]{' '.join(shlex.quote(x) for x in pi_cmd[:5])} ...[/bright_cyan]"
                )
                console.print(
                    f"  [dim]└─ 📌 若拉起 Google agy，Mailuo 托管命令:[/dim] [bright_magenta]{' '.join(shlex.quote(x) for x in agy_cmd[:3])} ...[/bright_magenta]"
                )

        console.print("\n")
        print_sessions_table(engine)


def run_repl(db_path: str, default_agent: str = "mailuo") -> None:
    engine = MailuoEngine(db_path=db_path)
    current_agent = default_agent

    console.print(
        Panel.fit(
            "[bold cyan]Mailuo (脉络) - 跨 Agent 交互式控制台 (支持 pi.dev & Google agy)[/bold cyan]\n"
            f"当前默认对话后端: [bold green]{current_agent}[/bold green]\n"
            "快捷指令：\n"
            "  [bold]/pi <提问>[/bold]     用 [bold]pi.dev[/bold] 回答并按当前话题脉络自动记录 (绑定专属 `--session-id`，穿插提问自动 `--no-session`)\n"
            "  [bold]/pi-tui[/bold]        直接拉起 [bold]pi.dev 交互式 TUI[/bold] 接续当前话题，退出后自动同步记录回 Mailuo\n"
            "  [bold]/agy <提问>[/bold]    用 [bold]Google Antigravity (`agy`)[/bold] 回答并共享当前话题脉络\n"
            "  [bold]/agy-tui[/bold]       直接拉起 [bold]Google `agy` 交互式界面[/bold] 接续当前话题\n"
            "  [bold]/agent <名称>[/bold]  切换默认对话后端 (`mailuo` | `pi` | `agy`)\n"
            "  [bold]/sync[/bold]          一键从本机 `~/.pi/agent/sessions` 和 `~/.gemini/antigravity-cli/brain` 导入历史会话\n"
            "  [bold]/list[/bold]          查看所有具体话题 Sessions 及其绑定的 pi/agy 会话 ID\n"
            "  [bold]/themes[/bold]        把所有跨 Agent 的 Session 整理成跟你有关的最核心主题\n"
            "  [bold]/btw <提问>[/bold]    强制以临时穿插隔离模式提问（绝不污染当前话题）\n"
            "  [bold]/quit[/bold]          退出",
            border_style="cyan",
        )
    )

    while True:
        active = engine.get_active_session()
        topic_label = active.title if active else "无激活话题"
        prompt_prefix = f"[{current_agent} | {topic_label}] > "
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
        if text.startswith("/sync"):
            parts = text.split()
            src = parts[1] if len(parts) > 1 else "all"
            imported = engine.sync_external_sessions(source=src, limit_per_source=10)
            console.print(f"[bold green]已成功从 {src} 导入 {len(imported)} 个原生会话到脉络库！[/bold green]")
            print_sessions_table(engine)
            continue
        if text.startswith("/agent "):
            target = text[7:].strip().lower()
            if target in ("mailuo", "pi", "agy"):
                current_agent = target
                console.print(f"[bold green]已切换默认对话 Agent 为: {current_agent}[/bold green]")
            else:
                console.print("[red]可选值: mailuo | pi | agy[/red]")
            continue
        if text == "/pi-tui":
            if not active:
                active = engine.create_topic_session(title="pi 交互式探讨话题", tags=["pi.dev"])
            console.print(f"[cyan]正在拉起 pi.dev 交互式会话，绑定话题《{active.title}》...[/cyan]")
            res = engine.chat(
                "进入 pi 交互式会话",
                agent_backend="pi",
                interactive_agent=True,
            )
            render_turn("/pi-tui", res)
            continue
        if text == "/agy-tui":
            if not active:
                active = engine.create_topic_session(title="Google agy 交互式探讨话题", tags=["agy"])
            console.print(f"[cyan]正在拉起 Google agy 交互式会话，绑定话题《{active.title}》...[/cyan]")
            res = engine.chat(
                "进入 agy 交互式会话",
                agent_backend="agy",
                interactive_agent=True,
            )
            render_turn("/agy-tui", res)
            continue
        if text.startswith("/pi "):
            q = text[4:].strip()
            res = engine.chat(q, agent_backend="pi")
            render_turn(q, res)
            continue
        if text.startswith("/agy "):
            q = text[5:].strip()
            res = engine.chat(q, agent_backend="agy")
            render_turn(q, res)
            continue
        if text.startswith("/new "):
            res = engine.chat(text[5:].strip(), agent_backend=current_agent, force_new_topic=True)
            render_turn(text, res)
            continue
        if text.startswith("/btw "):
            res = engine.chat(text[5:].strip(), agent_backend=current_agent, force_tangent=True)
            render_turn(text, res)
            continue
        if text.startswith("/recall "):
            q = text[8:].strip()
            recalled = engine.recall_by_query(q)
            if recalled:
                console.print(
                    f"[bold magenta]已调出并切换至话题：[/bold magenta]《{recalled.title}》 ({recalled.id})"
                )
                console.print(f"[dim]摘要：{recalled.summary}[/dim]")
            else:
                console.print("[red]未找到匹配的历史话题 Session[/red]")
            continue

        res = engine.chat(text, agent_backend=current_agent)
        render_turn(text, res)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Mailuo (脉络): 跨 pi.dev 与 Google agy 的话题 Session 与核心主题管理系统"
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="运行完整场景演示（含导入本机 pi/agy 真实会话、穿插隔离、跨 Agent 调出话题与整理核心主题）",
    )
    parser.add_argument(
        "--sync",
        choices=["all", "pi", "agy"],
        help="直接扫描并导入本机 ~/.pi/agent/sessions 与 ~/.gemini/antigravity-cli/brain 的历史会话",
    )
    parser.add_argument(
        "--agent",
        choices=["mailuo", "pi", "agy"],
        default="mailuo",
        help="默认使用的 Agent 后端 (mailuo | pi | agy)",
    )
    parser.add_argument(
        "--ask",
        type=str,
        help="单次直接提问（会自动按脉络路由并调用指定的 --agent）",
    )
    parser.add_argument(
        "--spawn",
        choices=["pi", "agy"],
        help="在当前激活的具体话题下直接拉起 pi 或 agy 的原生交互式 TUI，并在退出后自动把对话记录进脉络",
    )
    parser.add_argument(
        "--db",
        default="~/.mailuo/mailuo.db",
        help="SQLite 数据库存储路径 (默认: ~/.mailuo/mailuo.db)",
    )
    args = parser.parse_args()

    if args.demo:
        run_demo()
        return

    engine = MailuoEngine(db_path=args.db)

    if args.sync:
        imported = engine.sync_external_sessions(source=args.sync, limit_per_source=15)
        console.print(f"[bold green]已成功从 {args.sync} 导入 {len(imported)} 个原生会话！[/bold green]")
        print_sessions_table(engine)
        return

    if args.spawn:
        active = engine.get_active_session()
        if not active:
            active = engine.create_topic_session(
                title=f"{args.spawn} 专题讨论", tags=[args.spawn]
            )
        res = engine.chat(
            f"拉起 {args.spawn} 交互式会话",
            agent_backend=args.spawn,
            interactive_agent=True,
        )
        render_turn(f"--spawn {args.spawn}", res)
        return

    if args.ask:
        res = engine.chat(args.ask, agent_backend=args.agent)
        render_turn(args.ask, res)
        return

    run_repl(args.db, default_agent=args.agent)


if __name__ == "__main__":
    main()
