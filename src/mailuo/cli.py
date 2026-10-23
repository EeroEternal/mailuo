"""Simplified Command-Line Interface for Mailuo (`mai` — Multi-User Cloudflare D1 Storage).

Usage:
  mai register [username]  # Register a new account (strictly isolated in Cloudflare D1)
  mai login [username]     # Log into an existing account on any machine
  mai whoami               # Show current user & cloud connection status
  mai                      # Start interactive REPL
  mai "<message>"          # Send a single message (auto-route / side-tangent / recall / consolidate)
  mai pi [prompt]          # Launch pi.dev under current topic context (auto-sync on exit)
  mai agy [prompt]         # Launch Google Antigravity (`agy`) under current topic context
  mai ls                   # List all topic sessions under current account
  mai themes               # Consolidate scattered sessions into Core Themes
  mai sync                 # Scan & upload local pi.dev and agy sessions to Cloudflare D1
"""

from __future__ import annotations

import difflib
import getpass
import os
import readline  # noqa: F401 — enable terminal arrow keys & history
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
    "scan": "sync",
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
    """Match exact or fuzzy short commands (e.g. `whooami` -> `whoami`)."""
    clean = cmd_word.lstrip("/").lower()
    if clean in KNOWN_COMMANDS:
        return KNOWN_COMMANDS[clean]
    if clean.isalpha() and len(clean) <= 10:
        matches = difflib.get_close_matches(clean, KNOWN_COMMANDS.keys(), n=1, cutoff=0.72)
        if matches:
            return KNOWN_COMMANDS[matches[0]]
    return None


def interactive_auth_flow(mode: str = "auto", preset_username: Optional[str] = None) -> dict:
    """Interactive registration / login flow for multi-tenant Cloudflare D1 isolation."""
    console.print(
        Panel.fit(
            "[bold cyan]🔐 mai Authentication[/bold cyan]\n"
            "Each account has an isolated workspace in Cloudflare D1. Log in with the same account on any machine to sync your sessions.",
            border_style="cyan",
        )
    )
    if mode == "auto":
        choice = (
            console.input(
                "[bold yellow]Select: (1) Register new account  (2) Login existing account [default 1]: [/bold yellow]"
            )
            .strip()
            .lower()
        )
        mode = "login" if choice in ("2", "login", "l") else "register"

    username = preset_username or console.input("[bold green]Username: [/bold green]").strip()
    password = getpass.getpass("Password (min 4 chars, hidden): ").strip()
    if not password:
        password = console.input("[bold green]Password (min 4 chars): [/bold green]").strip()

    if mode == "register":
        try:
            profile = register_user(username, password)
            console.print(
                f"[bold green]✅ Registered & logged in as: {profile['username']} ({profile['user_id']})[/bold green]"
            )
            return profile
        except RuntimeError as e:
            if "已被注册" in str(e) or "already" in str(e).lower():
                console.print(f"[yellow]{e} — attempting login instead...[/yellow]")
                profile = login_user(username, password)
                console.print(
                    f"[bold green]✅ Logged in as: {profile['username']} ({profile['user_id']})[/bold green]"
                )
                return profile
            raise
    else:
        profile = login_user(username, password)
        console.print(
            f"[bold green]✅ Logged in as: {profile['username']} ({profile['user_id']})[/bold green]"
        )
        return profile


def ensure_logged_in(db_target: str) -> None:
    """Prompt for registration/login if using Cloudflare D1 and no local credentials exist."""
    if db_target != "d1":
        return
    if load_auth_profile() is None and sys.stdin.isatty():
        console.print("[yellow]No active `mai` account found on this machine. Please register or log in:[/yellow]")
        interactive_auth_flow(mode="auto")


def render_turn(user_text: str, result: TurnResult, storage_label: str = "") -> None:
    console.print(f"\n[bold cyan]🧑 User[/bold cyan]: {escape(user_text)}")

    action_badge = {
        RouteActionType.NEW_TOPIC: "[bold green]🌱 New Topic Session[/bold green]",
        RouteActionType.CONTINUE: "[bold blue]🎯 Continue Topic[/bold blue]",
        RouteActionType.SIDE_TANGENT: "[bold yellow]🛡️ Side-Tangent Sandbox (Isolated)[/bold yellow]",
        RouteActionType.SWITCH_EXISTING: "[bold magenta]🔍 Recall Topic Session[/bold magenta]",
        RouteActionType.CONSOLIDATE: "[bold bright_cyan]🧠 Consolidate Core Themes[/bold bright_cyan]",
    }.get(result.decision.action, str(result.decision.action))

    sess_title = (
        f"[{escape(result.active_session.title)}] ({result.active_session.id})"
        if result.active_session
        else "Global View"
    )
    ext_bindings = ""
    if result.active_session and result.active_session.external_sessions:
        pairs = [f"{k}:{v[:8]}" for k, v in result.active_session.external_sessions.items()]
        ext_bindings = f" | Bound: [cyan]{', '.join(pairs)}[/cyan]"

    db_note = f" | ☁️ [dim]{escape(storage_label)}[/dim]" if storage_label else ""
    console.print(
        f"  [dim]└─ Route:[/dim] {action_badge} | [bold]{sess_title}[/bold]{ext_bindings}{db_note}\n"
        f"  [dim]└─ Reason:[/dim] [italic]{escape(result.decision.reason)}[/italic]"
    )
    if result.agent_execution:
        cmd_preview = " ".join(shlex.quote(c) for c in result.agent_execution.command[:6])
        if len(result.agent_execution.command) > 6:
            cmd_preview += " ..."
        console.print(
            f"  [dim]└─ External Agent ({result.agent_execution.agent_name}):[/dim] [green]{escape(cmd_preview)}[/green]"
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
        title=f"📂 Topic Sessions — {engine.storage.db_label}",
        show_lines=True,
    )
    table.add_column("Status", width=9)
    table.add_column("Session ID", style="cyan", no_wrap=True)
    table.add_column("Topic Title", style="bold")
    table.add_column("Agent", style="bright_magenta")
    table.add_column("Core Theme", style="green")
    table.add_column("Structured Summary (Clean Context)")

    for s in sessions:
        marker = "🟢 Active" if s.id == active_id else "⏸️ Paused"
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
            s.summary or "(No summary yet)",
        )
    console.print(table)


def run_demo() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        db_file = Path(tmpdir) / "demo_mailuo.db"
        engine = MailuoEngine(db_path=db_file, user_id="demo_alice")

        console.print(
            Panel.fit(
                "[bold bright_white]mai (Mailuo) Quick Demo[/bold bright_white]\n"
                "Multi-tenant Cloudflare D1 + `pi.dev` (`pi`) / `Google Antigravity` (`agy`) Session Management",
                border_style="bright_blue",
            )
        )

        script = [
            "Let's discuss the knowledge-base backend: Cloudflare D1 plus vector search for conversation data.",
            "We decided to split the core tables into tenant_topic_sessions and tenant_messages, isolated by user_id.",
            "By the way, write me a Python regex that matches email addresses without disturbing the architecture topic.",
            "Let's switch topics: how should agent session auto-routing and context anti-pollution be designed?",
            "Can you recall the session about the Cloudflare D1 vector-search backend we discussed earlier?",
            "Now consolidate all these sessions into the core themes that matter most to me.",
        ]

        for user_input in script:
            res = engine.chat(user_input)
            render_turn(user_input, res, engine.storage.db_label)

        console.print("\n")
        print_sessions_table(engine)


def run_repl(engine: MailuoEngine) -> None:
    console.print(
        Panel.fit(
            f"[bold cyan]mai[/bold cyan] | Storage: [bold green]{escape(engine.storage.db_label)}[/bold green]\n"
            "Type any message to chat (auto-detects side-tangents, recalls past sessions, or consolidates themes).\n"
            "Commands: [bold]ls[/bold] (sessions) | [bold]themes[/bold] (core themes) | [bold]pi[/bold] (launch pi) | [bold]agy[/bold] (launch agy) | [bold]sync[/bold] (scan & upload) | [bold]whoami[/bold] (account) | [bold]q / Ctrl+C[/bold] (exit)",
            border_style="cyan",
        )
    )

    cached_active: Optional[TopicSession] = None
    try:
        cached_active = engine.get_active_session()
    except Exception:
        cached_active = None

    while True:
        topic_label = cached_active.title[:18] if cached_active else "new-topic"
        prompt_markup = f"\n[bold green]{escape(f'[{engine.storage.username} | {topic_label}]')} > [/bold green]"
        try:
            text = console.input(prompt_markup).strip()
        except (EOFError, KeyboardInterrupt):
            console.print("\n[dim]👋 Bye![/dim]")
            break

        if not text:
            continue

        parts = text.split(maxsplit=1)
        first_word = parts[0]
        rest_arg = parts[1].strip() if len(parts) > 1 else ""
        matched_cmd = (
            resolve_short_command(first_word)
            if not rest_arg or first_word.lower().lstrip("/") in ("pi", "agy", "new", "rm", "del", "sync", "scan")
            else None
        )

        try:
            if matched_cmd == "quit":
                console.print("[dim]👋 Bye![/dim]")
                break

            if matched_cmd == "help":
                console.print(Panel.fit(HELP_TEXT, border_style="cyan"))
                continue

            if matched_cmd == "whoami":
                console.print(
                    f"[bold cyan]👤 Current User:[/bold cyan] {engine.storage.username} ({engine.storage.user_id}) | [bold green]{escape(engine.storage.db_label)}[/bold green]"
                )
                continue

            if matched_cmd == "ls":
                print_sessions_table(engine)
                continue

            if matched_cmd == "themes":
                res = engine.chat("Consolidate all sessions into core themes")
                render_turn("themes", res, engine.storage.db_label)
                print_sessions_table(engine)
                continue

            if matched_cmd == "sync":
                force_flag = "--force" in rest_arg or "-f" in rest_arg
                clean_src = rest_arg.replace("--force", "").replace("-f", "").strip()
                src = clean_src if clean_src in ("all", "pi", "agy") else "all"
                console.print(f"[cyan]🔍 Scanning local ({src}) sessions for pi.dev & agy and uploading to cloud...[/cyan]")
                imported = engine.sync_external_sessions(
                    source=src, limit_per_source=50, force_update=force_flag
                )
                console.print(
                    f"[bold green]☁️ Sync complete! Uploaded/updated {len(imported)} session(s) to {escape(engine.storage.db_label)} (unchanged sessions skipped; use --force to recompute all).[/bold green]"
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
                    console.print("[bold green]🌱 Active topic reset. Next turn will start a fresh topic.[/bold green]")
                continue

            if matched_cmd == "rm" and rest_arg:
                engine.storage.delete_session(rest_arg)
                if cached_active and cached_active.id == rest_arg:
                    cached_active = None
                console.print(f"[bold yellow]🗑️ Deleted topic session: {escape(rest_arg)}[/bold yellow]")
                continue

            if matched_cmd == "pi":
                if rest_arg.startswith("-"):
                    sess = engine.launch_agent_interactive("pi", extra_args=shlex.split(rest_arg))
                    if sess:
                        cached_active = sess
                        console.print(
                            f"[bold green]✅ Synced pi.dev discussion to topic [{escape(sess.title)}] ({sess.id})[/bold green]"
                        )
                elif rest_arg:
                    res = engine.chat(rest_arg, agent_backend="pi")
                    cached_active = res.active_session
                    render_turn(text, res, engine.storage.db_label)
                else:
                    sess = engine.launch_agent_interactive("pi")
                    if sess:
                        cached_active = sess
                        console.print(
                            f"[bold green]✅ Synced pi.dev discussion to topic [{escape(sess.title)}] ({sess.id})[/bold green]"
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
                            f"[bold green]✅ Synced Google agy discussion to topic [{escape(sess.title)}] ({sess.id})[/bold green]"
                        )
                continue

            if not rest_arg and text.isascii() and text.isalpha() and len(text) <= 8:
                console.print(
                    f"[yellow]Unknown command `{escape(text)}`. Type `help` for commands, or enter a full sentence to discuss a topic.[/yellow]"
                )
                continue

            res = engine.chat(text)
            cached_active = res.active_session
            render_turn(text, res, engine.storage.db_label)

        except KeyboardInterrupt:
            console.print("\n[yellow]⚡ Operation cancelled (press Ctrl+C again or type q to exit)[/yellow]")
            continue


HELP_TEXT = """[bold cyan]mai (Mailuo) — Multi-User Cloud Session & Theme CLI (Cloudflare D1)[/bold cyan]

Account & Multi-Machine Sync:
  [bold green]mai register [user] [pass][/bold green]    Register a personal account (strictly isolated by user_id)
  [bold green]mai login [user] [pass][/bold green]       Log in on a new machine to access your cloud sessions
  [bold green]mai whoami[/bold green]                    Show current logged-in account and cloud gateway status
  [bold green]mai logout[/bold green]                    Clear local credentials on this machine

Session & Agent Commands:
  [bold green]mai[/bold green]                           Start interactive REPL
  [bold green]mai "<message>"[/bold green]               Send a message (auto-route / side-tangent / recall / consolidate)
  [bold green]mai pi [prompt][/bold green]               Launch [bold]pi.dev[/bold] under current topic (auto-extracts summary on exit)
  [bold green]mai agy [prompt][/bold green]              Launch [bold]Google agy[/bold] under current topic (auto-extracts summary on exit)
  [bold green]mai ls[/bold green]                        List all topic sessions under current account
  [bold green]mai new [message][/bold green]             Start a new topic session (or reset active pointer)
  [bold green]mai rm <session_id>[/bold green]           Delete a specific topic session
  [bold green]mai themes[/bold green]                    Consolidate scattered sessions into Core Themes
  [bold green]mai sync [pi|agy] [--force][/bold green]   Scan & upload local pi.dev and agy sessions to Cloudflare D1
  [bold green]mai demo[/bold green]                      Run automated demonstration
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

    if cmd in ("register", "reg", "signup"):
        if len(args) >= 3:
            profile = register_user(args[1], args[2])
            console.print(
                f"[bold green]✅ Registered & logged in as: {profile['username']} ({profile['user_id']})[/bold green]"
            )
        else:
            preset_u = args[1] if len(args) == 2 else None
            interactive_auth_flow(mode="register", preset_username=preset_u)
        return

    if cmd in ("login", "signin"):
        if len(args) >= 3:
            profile = login_user(args[1], args[2])
            console.print(
                f"[bold green]✅ Logged in as: {profile['username']} ({profile['user_id']})[/bold green]"
            )
        else:
            preset_u = args[1] if len(args) == 2 else None
            interactive_auth_flow(mode="login", preset_username=preset_u)
        return

    if cmd in ("logout",):
        clear_auth_profile()
        console.print("[bold yellow]Cleared local auth credentials.[/bold yellow]")
        return

    resolved = resolve_short_command(cmd) if not rest else None

    if cmd in ("whoami", "me") or resolved == "whoami":
        prof = load_auth_profile()
        if prof:
            console.print(
                f"[bold green]👤 User:[/bold green] {prof['username']} (ID: {prof['user_id']})\n"
                f"[bold cyan]☁️ Cloud Gateway:[/bold cyan] {prof.get('gateway_url')}"
            )
        else:
            console.print("[yellow]Not logged in. Run `mai register` or `mai login`.[/yellow]")
        return

    ensure_logged_in(db_target)
    engine = MailuoEngine(db_path=db_target)

    if cmd in ("ls", "list", "--list") or resolved == "ls":
        print_sessions_table(engine)
        return

    if cmd in ("rm", "del", "delete") and rest:
        engine.storage.delete_session(rest)
        console.print(f"[bold yellow]🗑️ Deleted topic session: {escape(rest)}[/bold yellow]")
        print_sessions_table(engine)
        return

    if cmd in ("new", "--new") or resolved == "new":
        if rest:
            res = engine.chat(rest, force_new_topic=True)
            render_turn(f"mai new {rest}", res, engine.storage.db_label)
        else:
            engine.storage.set_active_session_id(None)
            console.print("[bold green]🌱 Reset active topic pointer. Next chat or `mai pi` / `mai agy` will start a fresh topic.[/bold green]")
        return

    if cmd in ("themes", "theme", "--themes") or resolved == "themes":
        res = engine.chat("Consolidate all sessions into core themes")
        render_turn("mai themes", res, engine.storage.db_label)
        print_sessions_table(engine)
        return

    if cmd in ("sync", "scan", "--sync", "--scan") or resolved == "sync":
        force_flag = "--force" in rest or "-f" in rest
        clean_src = rest.replace("--force", "").replace("-f", "").strip()
        src = clean_src if clean_src in ("all", "pi", "agy") else "all"
        console.print(f"[cyan]🔍 Scanning local ({src}) sessions for pi.dev & agy and uploading to cloud...[/cyan]")
        imported = engine.sync_external_sessions(
            source=src, limit_per_source=50, force_update=force_flag
        )
        console.print(
            f"[bold green]☁️ Sync complete! Uploaded/updated {len(imported)} local ({src}) session(s) to {escape(engine.storage.db_label)} (unchanged sessions skipped; use --force to recompute all).[/bold green]"
        )
        print_sessions_table(engine)
        return

    if cmd == "pi":
        if rest.startswith("-"):
            sess = engine.launch_agent_interactive("pi", extra_args=shlex.split(rest))
            if sess:
                console.print(
                    f"[bold green]✅ Synced pi.dev discussion to topic [{escape(sess.title)}] ({sess.id}) — {escape(engine.storage.db_label)}[/bold green]"
                )
        elif rest:
            res = engine.chat(rest, agent_backend="pi")
            render_turn(f"mai pi {rest}".strip(), res, engine.storage.db_label)
        else:
            sess = engine.launch_agent_interactive("pi")
            if sess:
                console.print(
                    f"[bold green]✅ Synced pi.dev discussion to topic [{escape(sess.title)}] ({sess.id}) — {escape(engine.storage.db_label)}[/bold green]"
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
                    f"[bold green]✅ Synced Google agy discussion to topic [{escape(sess.title)}] ({sess.id}) — {escape(engine.storage.db_label)}[/bold green]"
                )
        return

    full_input = " ".join(args).strip()
    res = engine.chat(full_input)
    render_turn(full_input, res, engine.storage.db_label)


def main() -> None:
    try:
        _main_inner()
    except KeyboardInterrupt:
        console.print("\n[dim]👋 Exited mai.[/dim]")
        sys.exit(0)


if __name__ == "__main__":
    main()
