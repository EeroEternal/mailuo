"""Simplified Command-Line Interface for Mailuo (脉络 `mai` — 多用户注册隔离 + Cloudflare D1 云端存储).

极简命令用法：
  mai register [用户名]    # 注册个人专属脉络账号（数据按账号严格隔离在 Cloudflare D1）
  mai login [用户名]       # 在新机器上登录已有账号，立即同步恢复所有话题脉络
  mai whoami               # 查看当前登录的账号与云端连接状态
  mai                      # 进入交互式对话（若未注册会自动引导注册/登录）
  mai "一句话或问题"       # 直接对话（自动判断延续话题 / 临时穿插隔离 / 调出旧话题 / 整理核心主题）
  mai pi [提示词]          # 在当前话题脉络下拉起 pi.dev (退出后自动同步记录到当前账号的 Cloudflare D1)
  mai agy [提示词]         # 在当前话题脉络下拉起 Google Antigravity (`agy`)
  mai ls                   # 查看当前账号下的所有话题 Sessions 列表
  mai themes               # 一键把所有零散 Session 整理为核心主题 (Core Themes)
  mai sync                 # 一键把本机 pi.dev 和 agy 的历史记录同步到当前账号的 Cloudflare D1
"""

from __future__ import annotations

import difflib
import getpass
import os
import readline  # noqa: F401 — 启用终端方向键与历史记录支持
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.markup import escape
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .engine import MailuoEngine, TurnResult
from .models import RouteActionType, TopicSession
from .storage import (
    clear_auth_profile,
    load_auth_profile,
    login_user,
    register_user,
)

console = Console()

KNOWN_COMMANDS = {
    "ls": "ls",
    "list": "ls",
    "ll": "ls",
    "themes": "themes",
    "theme": "themes",
    "sync": "sync",
    "whoami": "whoami",
    "who": "whoami",
    "me": "whoami",
    "pi": "pi",
    "agy": "agy",
    "new": "new",
    "rm": "rm",
    "del": "rm",
    "help": "help",
    "?": "help",
    "q": "quit",
    "quit": "quit",
    "exit": "quit",
}


def resolve_short_command(cmd_word: str) -> Optional[str]:
    """精确或容错匹配短命令（例如将打错的 `whooami` 自动识别为 `whoami`）."""
    clean = cmd_word.lstrip("/").lower()
    if clean in KNOWN_COMMANDS:
        return KNOWN_COMMANDS[clean]
    # 针对纯英文短词做编辑距离容错（如 whooami -> whoami, theems -> themes）
    if clean.isalpha() and len(clean) <= 10:
        matches = difflib.get_close_matches(clean, KNOWN_COMMANDS.keys(), n=1, cutoff=0.72)
        if matches:
            return KNOWN_COMMANDS[matches[0]]
    return None


def interactive_auth_flow(mode: str = "auto", preset_username: Optional[str] = None) -> dict:
    """引导用户完成注册或登录，确保每个人的数据在 Cloudflare D1 中独立隔离."""
    console.print(
        Panel.fit(
            "[bold cyan]🔐 Mailuo (`mai`) 账号注册 / 登录[/bold cyan]\n"
            "每个账号在 Cloudflare D1 云端拥有完全独立的脉络空间，不同机器登录同一账号即可无缝接续，不同人的数据绝不串号。",
            border_style="cyan",
        )
    )
    if mode == "auto":
        choice = (
            console.input(
                "[bold yellow]请选择: (1) 注册新账号  (2) 登录已有账号 [默认 1]: [/bold yellow]"
            )
            .strip()
            .lower()
        )
        mode = "login" if choice in ("2", "login", "l") else "register"

    username = preset_username or console.input("[bold green]用户名 (username): [/bold green]").strip()
    password = getpass.getpass("密码 (至少4位，输入不回显): ").strip()
    if not password:
        password = console.input("[bold green]密码 (至少4位): [/bold green]").strip()

    if mode == "register":
        try:
            profile = register_user(username, password)
            console.print(
                f"[bold green]✅ 注册成功！当前登录账号: {profile['username']} ({profile['user_id']})[/bold green]"
            )
            return profile
        except RuntimeError as e:
            if "已被注册" in str(e):
                console.print(f"[yellow]{e}，正在尝试为您直接登录...[/yellow]")
                profile = login_user(username, password)
                console.print(
                    f"[bold green]✅ 登录成功！当前账号: {profile['username']} ({profile['user_id']})[/bold green]"
                )
                return profile
            raise
    else:
        profile = login_user(username, password)
        console.print(
            f"[bold green]✅ 登录成功！当前账号: {profile['username']} ({profile['user_id']})[/bold green]"
        )
        return profile


def ensure_logged_in(db_target: str) -> None:
    """当使用云端 D1 且本机尚未注册/登录时，自动触发引导注册流程."""
    if db_target != "d1":
        return
    if load_auth_profile() is None and sys.stdin.isatty():
        console.print("[yellow]检测到当前机器尚未登录 `mai` 脉络账号，请先完成快速注册或登录：[/yellow]")
        interactive_auth_flow(mode="auto")


def render_turn(user_text: str, result: TurnResult, storage_label: str = "") -> None:
    console.print(f"\n[bold cyan]🧑 用户[/bold cyan]: {escape(user_text)}")

    action_badge = {
        RouteActionType.NEW_TOPIC: "[bold green]🌱 新建话题 Session[/bold green]",
        RouteActionType.CONTINUE: "[bold blue]🎯 延续当前话题[/bold blue]",
        RouteActionType.SIDE_TANGENT: "[bold yellow]🛡️ 临时穿插隔离 (Side-Tangent)[/bold yellow]",
        RouteActionType.SWITCH_EXISTING: "[bold magenta]🔍 语义召回历史 Session[/bold magenta]",
        RouteActionType.CONSOLIDATE: "[bold bright_cyan]🧠 核心主题归纳整理[/bold bright_cyan]",
    }.get(result.decision.action, str(result.decision.action))

    sess_title = (
        f"《{escape(result.active_session.title)}》 ({result.active_session.id})"
        if result.active_session
        else "全局视图"
    )
    ext_bindings = ""
    if result.active_session and result.active_session.external_sessions:
        pairs = [f"{k}:{v[:8]}" for k, v in result.active_session.external_sessions.items()]
        ext_bindings = f" | 绑定: [cyan]{', '.join(pairs)}[/cyan]"

    db_note = f" | ☁️ [dim]{escape(storage_label)}[/dim]" if storage_label else ""
    console.print(
        f"  [dim]└─ 脉络路由:[/dim] {action_badge} | [bold]{sess_title}[/bold]{ext_bindings}{db_note}\n"
        f"  [dim]└─ 路由依据:[/dim] [italic]{escape(result.decision.reason)}[/italic]"
    )
    if result.agent_execution:
        cmd_preview = " ".join(shlex.quote(c) for c in result.agent_execution.command[:6])
        if len(result.agent_execution.command) > 6:
            cmd_preview += " ..."
        console.print(
            f"  [dim]└─ 外部 Agent ({result.agent_execution.agent_name}):[/dim] [green]{escape(cmd_preview)}[/green]"
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
        title=f"📂 话题 Sessions 列表 — {engine.storage.db_label}",
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
        engine = MailuoEngine(db_path=db_file, user_id="demo_alice")

        console.print(
            Panel.fit(
                "[bold bright_white]mai (脉络 Mailuo) 极简演示[/bold bright_white]\n"
                "多用户云端隔离 (Cloudflare D1) + `pi.dev` (`pi`) / `Google Antigravity` (`agy`) 托管",
                border_style="bright_blue",
            )
        )

        script = [
            "我想讨论一下我们的知识库后端架构，准备采用 Cloudflare D1 配合向量检索来存对话数据。",
            "我们决定核心表拆成 tenant_topic_sessions 和 tenant_messages 两张表，并按 user_id 隔离。",
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
            f"[bold cyan]mai (脉络)[/bold cyan] | 存储空间: [bold green]{escape(engine.storage.db_label)}[/bold green]\n"
            "直接输入任何话即可（支持自动识别『顺便问一下...』、『把上次讨论XX的话题调出来』、『整理核心主题』）。\n"
            "极简命令: [bold]ls[/bold] (话题列表) | [bold]themes[/bold] (整理核心主题) | [bold]pi[/bold] (拉起pi) | [bold]agy[/bold] (拉起agy) | [bold]sync[/bold] (同步历史) | [bold]whoami[/bold] (当前账号) | [bold]q / Ctrl+C[/bold] (退出)",
            border_style="cyan",
        )
    )

    # 缓存当前激活的话题，避免每次打印提示符都发起网络请求导致卡顿
    cached_active: Optional[TopicSession] = None
    try:
        cached_active = engine.get_active_session()
    except Exception:
        cached_active = None

    while True:
        topic_label = cached_active.title[:18] if cached_active else "新话题"
        # 注意：使用 escape 避免 rich.console.input 把 `[...]` 当作样式标签吞掉！
        prompt_markup = f"\n[bold green]{escape(f'[{engine.storage.username} | {topic_label}]')} > [/bold green]"
        try:
            text = console.input(prompt_markup).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]👋 再见！[/dim]")
            break

        if not text:
            continue

        parts = text.split(maxsplit=1)
        first_word = parts[0]
        rest_arg = parts[1].strip() if len(parts) > 1 else ""
        matched_cmd = resolve_short_command(first_word) if not rest_arg or first_word.lower().lstrip("/") in ("pi", "agy", "new", "rm", "del", "sync") else None

        try:
            if matched_cmd == "quit":
                console.print("[dim]👋 再见！[/dim]")
                break

            if matched_cmd == "help":
                console.print(Panel.fit(HELP_TEXT, border_style="cyan"))
                continue

            if matched_cmd == "whoami":
                console.print(
                    f"[bold cyan]👤 当前用户:[/bold cyan] {engine.storage.username} ({engine.storage.user_id}) | [bold green]{escape(engine.storage.db_label)}[/bold green]"
                )
                continue

            if matched_cmd == "ls":
                print_sessions_table(engine)
                continue

            if matched_cmd == "themes":
                res = engine.chat("请把这些 session 整理成跟我有关的最核心主题")
                render_turn("themes", res, engine.storage.db_label)
                print_sessions_table(engine)
                continue

            if matched_cmd == "sync":
                src = rest_arg if rest_arg in ("all", "pi", "agy") else "all"
                imported = engine.sync_external_sessions(source=src, limit_per_source=10)
                console.print(
                    f"[bold green]☁️ 已同步 {len(imported)} 个本机 ({src}) 会话到 {escape(engine.storage.db_label)}！[/bold green]"
                )
                print_sessions_table(engine)
                continue

            if matched_cmd == "new":
                if rest_arg:
                    res = engine.chat(rest_arg, force_new_topic=True)
                    cached_active = res.active_session
                    render_turn(text, res, engine.storage.db_label)
                else:
                    engine.storage.set_active_session_id(None)
                    cached_active = None
                    console.print("[bold green]🌱 已重置为空白话题状态。[/bold green]")
                continue

            if matched_cmd == "rm" and rest_arg:
                engine.storage.delete_session(rest_arg)
                if cached_active and cached_active.id == rest_arg:
                    cached_active = None
                console.print(f"[bold yellow]🗑️ 已删除话题 Session: {escape(rest_arg)}[/bold yellow]")
                continue

            if matched_cmd == "pi":
                if rest_arg:
                    res = engine.chat(rest_arg, agent_backend="pi")
                    cached_active = res.active_session
                    render_turn(text, res, engine.storage.db_label)
                else:
                    sess = engine.launch_agent_interactive("pi")
                    if sess:
                        cached_active = sess
                        console.print(
                            f"[bold green]✅ 已将刚才在 pi.dev 中的讨论同步至话题《{escape(sess.title)}》({sess.id})[/bold green]"
                        )
                continue

            if matched_cmd == "agy":
                if rest_arg:
                    res = engine.chat(rest_arg, agent_backend="agy")
                    cached_active = res.active_session
                    render_turn(text, res, engine.storage.db_label)
                else:
                    sess = engine.launch_agent_interactive("agy")
                    if sess:
                        cached_active = sess
                        console.print(
                            f"[bold green]✅ 已将刚才在 Google agy 中的讨论同步至话题《{escape(sess.title)}》({sess.id})[/bold green]"
                        )
                continue

            # 防止用户随手敲错单个短英文单词（比如 `lls`, `whoo`）被误当成新话题发往云端
            if not rest_arg and text.isascii() and text.isalpha() and len(text) <= 8:
                console.print(
                    f"[yellow]未识别的短命令 `{escape(text)}`。输入 `help` 查看命令列表，或输入完整句子开始讨论话题。[/yellow]"
                )
                continue

            res = engine.chat(text)
            cached_active = res.active_session
            render_turn(text, res, engine.storage.db_label)

        except KeyboardInterrupt:
            console.print("\n[yellow]⚡ 已取消当前操作（再按一次 Ctrl+C 或输入 q 即可退出 mai）[/yellow]")
            continue


HELP_TEXT = """[bold cyan]mai (脉络 Mailuo) — 多用户云端隔离极简 CLI (Cloudflare D1)[/bold cyan]

账号与多机同步:
  [bold green]mai register [用户名] [密码][/bold green]  注册个人专属账号（数据按账号严格隔离，绝不串号）
  [bold green]mai login [用户名] [密码][/bold green]     在其他机器上登录账号，立即接入你的个人云端脉络
  [bold green]mai whoami[/bold green]                    查看当前登录的账号与云端连接状态
  [bold green]mai logout[/bold green]                    退出当前机器上的登录状态

日常脉络操作:
  [bold green]mai[/bold green]                           启动交互式对话
  [bold green]mai "<任意对话内容>"[/bold green]          直接发送一句话（自动路由/穿插隔离/调出历史话题/整理主题）
  [bold green]mai pi [提问内容][/bold green]             使用 [bold]pi.dev[/bold] 来讨论话题（退出后自动提取你聊的内容生成脉络摘要）
  [bold green]mai agy [提问内容][/bold green]            使用 [bold]Google agy[/bold] 来讨论话题（退出后自动提取你聊的内容生成脉络摘要）
  [bold green]mai ls[/bold green]                        查看当前账号下的所有具体话题 Sessions
  [bold green]mai new [新话题内容][/bold green]          开启全新话题（或重置当前激活指针）
  [bold green]mai rm <session_id>[/bold green]           删除指定的话题 Session
  [bold green]mai themes[/bold green]                    把当前账号下的零散 Session 整理成『最核心主题』
  [bold green]mai sync[/bold green]                      将本机 pi.dev 和 agy 的历史记录一键同步至当前账号下
  [bold green]mai demo[/bold green]                      运行自动化演示
"""


def _main_inner() -> None:
    db_target = os.getenv("MAILUO_DB", "d1")
    args = sys.argv[1:]

    if not args:
        ensure_logged_in(db_target)
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

    # 账号注册 / 登录 / 状态查询
    if cmd in ("register", "reg", "signup"):
        if len(args) >= 3:
            profile = register_user(args[1], args[2])
            console.print(
                f"[bold green]✅ 注册并登录成功！当前用户: {profile['username']} ({profile['user_id']})[/bold green]"
            )
        else:
            preset_u = args[1] if len(args) == 2 else None
            interactive_auth_flow(mode="register", preset_username=preset_u)
        return

    if cmd in ("login", "signin"):
        if len(args) >= 3:
            profile = login_user(args[1], args[2])
            console.print(
                f"[bold green]✅ 登录成功！当前用户: {profile['username']} ({profile['user_id']})[/bold green]"
            )
        else:
            preset_u = args[1] if len(args) == 2 else None
            interactive_auth_flow(mode="login", preset_username=preset_u)
        return

    if cmd in ("logout",):
        clear_auth_profile()
        console.print("[bold yellow]已清除本机登录凭证。[/bold yellow]")
        return

    resolved = resolve_short_command(cmd) if not rest else None

    if cmd in ("whoami", "me") or resolved == "whoami":
        prof = load_auth_profile()
        if prof:
            console.print(
                f"[bold green]👤 当前登录账号:[/bold green] {prof['username']} (ID: {prof['user_id']})\n"
                f"[bold cyan]☁️ 云端网关:[/bold cyan] {prof.get('gateway_url')}"
            )
        else:
            console.print("[yellow]当前未登录云端账号。请运行 `mai register` 或 `mai login`。[/yellow]")
        return

    ensure_logged_in(db_target)
    engine = MailuoEngine(db_path=db_target)

    if cmd in ("ls", "list", "--list") or resolved == "ls":
        print_sessions_table(engine)
        return

    if cmd in ("rm", "del", "delete") and rest:
        engine.storage.delete_session(rest)
        console.print(f"[bold yellow]🗑️ 已删除话题 Session: {escape(rest)}[/bold yellow]")
        print_sessions_table(engine)
        return

    if cmd in ("new", "--new") or resolved == "new":
        if rest:
            res = engine.chat(rest, force_new_topic=True)
            render_turn(f"mai new {rest}", res, engine.storage.db_label)
        else:
            engine.storage.set_active_session_id(None)
            console.print("[bold green]🌱 已重置当前激活指针，下一次对话或拉起 `mai pi` / `mai agy` 将开启全新话题！[/bold green]")
        return

    if cmd in ("themes", "theme", "--themes") or resolved == "themes":
        res = engine.chat("请把这些 session 整理成跟我有关的最核心主题")
        render_turn("mai themes", res, engine.storage.db_label)
        print_sessions_table(engine)
        return

    if cmd in ("sync", "--sync") or resolved == "sync":
        src = args[1] if len(args) > 1 else "all"
        imported = engine.sync_external_sessions(source=src, limit_per_source=10)
        console.print(
            f"[bold green]☁️ 已成功将 {len(imported)} 个本机 ({src}) 会话同步至 {escape(engine.storage.db_label)}！[/bold green]"
        )
        print_sessions_table(engine)
        return

    if cmd == "pi":
        if rest:
            res = engine.chat(rest, agent_backend="pi")
            render_turn(f"mai pi {rest}".strip(), res, engine.storage.db_label)
        else:
            sess = engine.launch_agent_interactive("pi")
            if sess:
                console.print(
                    f"[bold green]✅ 已将刚才在 pi.dev 中的讨论同步至话题《{escape(sess.title)}》({sess.id}) — {escape(engine.storage.db_label)}[/bold green]"
                )
        return

    if cmd == "agy":
        if rest:
            res = engine.chat(rest, agent_backend="agy")
            render_turn(f"mai agy {rest}".strip(), res, engine.storage.db_label)
        else:
            sess = engine.launch_agent_interactive("agy")
            if sess:
                console.print(
                    f"[bold green]✅ 已将刚才在 Google agy 中的讨论同步至话题《{escape(sess.title)}》({sess.id}) — {escape(engine.storage.db_label)}[/bold green]"
                )
        return

    full_input = " ".join(args).strip()
    res = engine.chat(full_input)
    render_turn(full_input, res, engine.storage.db_label)


def main() -> None:
    try:
        _main_inner()
    except KeyboardInterrupt:
        console.print("\n[dim]👋 已退出 mai。[/dim]")
        sys.exit(0)


if __name__ == "__main__":
    main()
