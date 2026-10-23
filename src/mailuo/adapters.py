"""Multi-Agent Connectors & Transcript Syncers for pi.dev (`pi`) and Google Antigravity (`agy`).

核心原则：
`pi` 和 `agy` 只是与用户交流的 Agent 工具载体，绝不把 "pi" 或 "agy" 当作讨论话题内容！
1. 当用户直接运行 `mai agy` 或 `mai pi` 进入交互式 TUI 时：
   - 如果当前还没有已沉淀的具体话题上下文，直接干干净净地启动 `agy` 或 `pi`，不发送任何伪造的首句 Prompt，等你进去说第一句话。
   - 当你从 `agy` 或 `pi` 退出后，自动扫描刚产生或更新的本地会话日志 (`transcript.jsonl` / `*.jsonl`)，提取你在里面真正讨论的话题、生成真实标题与结构化摘要，同步到 Cloudflare D1！
2. 当你已经通过 `mai "调出上次讨论 XX 的话题"` 激活了一个有历史上下文的话题，再运行 `mai agy` 或 `mai pi` 时：
   - 自动恢复绑定的 `--conversation <id>` 或 `--session-id <uuid>`；若跨 Agent 切换则仅注入该话题的真实历史脉络摘要。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .models import CoreTheme, MessageRecord, TopicSession


@dataclass
class AgentExecutionResult:
    """外部 Agent (`pi` / `agy`) 执行或拉起后的返回结果."""

    agent_name: str
    external_session_id: Optional[str]
    command: list[str]
    reply: str
    returncode: int
    is_ephemeral: bool = False
    captured_messages: Optional[list[dict[str, str]]] = None


def has_meaningful_context(
    session: Optional[TopicSession],
    recent_messages: Optional[list[MessageRecord]] = None,
) -> bool:
    """判断当前 TopicSession 是否已经有真实讨论过的上下文（避免为空会话注入伪造的上下文头）."""
    if session is None:
        return False
    if session.summary or session.key_decisions or session.open_questions:
        return True
    if recent_messages and len(recent_messages) > 0:
        return True
    return False


def run_interactive(cmd: list[str], cwd: Optional[str | Path] = None) -> subprocess.Popen:
    """Run an agent TUI in the foreground and wait for it to exit.

    Ctrl+C reaches both mai and the agent (same foreground process group), so the
    SIGINT is swallowed here: dying mid-session would skip the transcript sync and
    leave the agent running.
    """
    proc = subprocess.Popen(cmd, cwd=str(cwd) if cwd else None)
    while proc.poll() is None:
        try:
            proc.wait()
        except KeyboardInterrupt:
            continue
    return proc


def build_mailuo_context_prompt(
    session: TopicSession,
    theme: Optional[CoreTheme] = None,
    recent_messages: Optional[list[MessageRecord]] = None,
    is_tangent: bool = False,
) -> str:
    """构建跨 Agent 共享的脉络上下文提示词（仅在确有历史讨论记录时生成）."""
    lines = [f"[Mailuo topic context] Topic under discussion: \"{session.title}\""]
    if theme:
        lines.append(
            f"[Core theme] {theme.name} — shared insight: {theme.consolidated_insight}"
        )
    if session.summary:
        lines.append(f"[Previous discussion summary] {session.summary}")
    if session.key_decisions:
        lines.append("[Key decisions] " + "; ".join(session.key_decisions))
    if session.open_questions:
        lines.append("[Open questions] " + "; ".join(session.open_questions))

    if recent_messages:
        cross_notes = []
        for m in recent_messages[-4:]:
            role_label = "User" if m.role == "user" else f"Agent({m.agent_source})"
            snippet = m.content.strip().replace("\n", " ")[:160]
            cross_notes.append(f"- {role_label}: {snippet}")
        if cross_notes:
            lines.append("[Recent turns]\n" + "\n".join(cross_notes))

    if is_tangent:
        lines.append(
            "[Note] This is a side-tangent question: answer concisely without altering the main topic's conclusions."
        )
    lines.append("[Language] Always reply in English.")
    return "\n".join(lines)


class PiDevBridge:
    """pi.dev (`pi`) 桥接器：支持拉起 `pi`（交互式 TUI 或非交互式 `-p`）并自动读取/同步其 JSONL 记录."""

    def __init__(
        self,
        pi_bin: Optional[str] = None,
        sessions_root: str | Path = "~/.pi/agent/sessions",
    ) -> None:
        self.pi_bin = pi_bin or shutil.which("pi") or "pi"
        self.sessions_root = Path(sessions_root).expanduser()

    def is_available(self) -> bool:
        return shutil.which(self.pi_bin) is not None or Path(self.pi_bin).exists()

    def ensure_pi_session_id(self, session: TopicSession) -> str:
        """为 Mailuo 的 TopicSession 绑定一个固定的 pi UUID."""
        if "pi" not in session.external_sessions:
            session.external_sessions["pi"] = str(uuid.uuid4())
        return session.external_sessions["pi"]

    def build_command(
        self,
        session: TopicSession,
        prompt: Optional[str] = None,
        theme: Optional[CoreTheme] = None,
        recent_messages: Optional[list[MessageRecord]] = None,
        is_tangent: bool = False,
        interactive: bool = False,
        extra_args: Optional[list[str]] = None,
    ) -> list[str]:
        """构建拉起 `pi` 的命令行参数.

        - 如果是正常话题推进：绑定 `--session-id <uuid>`。
        - 如果是临时穿插问题 (`is_tangent=True`)：自动附加 `--no-session` 隔离！
        - 只有当该话题确实已有历史沉淀内容时，才通过 `--append-system-prompt` 注入背景。
        """
        cmd = [self.pi_bin]

        # The caller may pick the session itself (e.g. `mai pi --session <id>` to resume).
        explicit_resume = any(
            a in ("--session", "--session-id", "--resume", "-r", "--continue", "-c")
            for a in (extra_args or [])
        )

        if is_tangent:
            cmd.append("--no-session")
        elif not explicit_resume:
            pi_sid = self.ensure_pi_session_id(session)
            cmd.extend(["--session-id", pi_sid])
            if session.title and not session.title.startswith(("未命名", "Unnamed")):
                cmd.extend(["--name", f"[Mailuo] {session.title}"])

        # A resumed pi session already carries its own history, so no extra context header.
        if (has_meaningful_context(session, recent_messages) or is_tangent) and not explicit_resume:
            context_prompt = build_mailuo_context_prompt(
                session, theme=theme, recent_messages=recent_messages, is_tangent=is_tangent
            )
            cmd.extend(["--append-system-prompt", context_prompt])

        if extra_args:
            cmd.extend(extra_args)

        if not interactive and prompt:
            cmd.extend(["--print", prompt])
        elif interactive and prompt:
            cmd.append(prompt)

        return cmd

    def run_turn(
        self,
        session: TopicSession,
        prompt: Optional[str] = None,
        theme: Optional[CoreTheme] = None,
        recent_messages: Optional[list[MessageRecord]] = None,
        is_tangent: bool = False,
        interactive: bool = False,
        cwd: Optional[str | Path] = None,
        timeout: int = 180,
        extra_args: Optional[list[str]] = None,
    ) -> AgentExecutionResult:
        """拉起 `pi` 执行对话，并在退出后自动提取真实对话内容回写到 Mailuo."""
        cmd = self.build_command(
            session=session,
            prompt=prompt,
            theme=theme,
            recent_messages=recent_messages,
            is_tangent=is_tangent,
            interactive=interactive,
            extra_args=extra_args,
        )
        # If the caller passed `--session <id>`, that is the session whose transcript we sync.
        pi_sid = session.external_sessions.get("pi")
        args = extra_args or []
        for flag in ("--session", "--session-id"):
            if flag in args:
                idx = args.index(flag)
                if idx + 1 < len(args) and not args[idx + 1].startswith("-"):
                    pi_sid = args[idx + 1]
                    session.external_sessions["pi"] = pi_sid
                break

        if interactive:
            proc = run_interactive(cmd, cwd=cwd)
            synced_msgs = self.read_pi_session_messages(pi_sid) if pi_sid else []
            last_assistant = next(
                (m["content"] for m in reversed(synced_msgs) if m["role"] == "assistant"),
                "(pi.dev interactive discussion finished)",
            )
            return AgentExecutionResult(
                agent_name="pi",
                external_session_id=pi_sid,
                command=cmd,
                reply=last_assistant,
                returncode=proc.returncode,
                is_ephemeral=is_tangent,
                captured_messages=synced_msgs,
            )

        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        reply = (proc.stdout or "").strip()
        if not reply and proc.stderr:
            reply = proc.stderr.strip()
        return AgentExecutionResult(
            agent_name="pi",
            external_session_id=pi_sid,
            command=cmd,
            reply=reply,
            returncode=proc.returncode,
            is_ephemeral=is_tangent,
        )

    def find_session_file(self, pi_session_id: str) -> Optional[Path]:
        if not self.sessions_root.exists():
            return None
        for path in self.sessions_root.rglob(f"*{pi_session_id}*.jsonl"):
            return path
        return None

    def list_recent_session_files(self, limit: int = 20) -> list[Path]:
        if not self.sessions_root.exists():
            return []
        files = list(self.sessions_root.rglob("*.jsonl"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:limit]

    @staticmethod
    def parse_pi_jsonl(file_path: Path) -> dict:
        session_id = file_path.stem.split("_")[-1]
        cwd = ""
        messages: list[dict[str, str]] = []

        with file_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = entry.get("type")
                if etype == "session":
                    session_id = entry.get("id", session_id)
                    cwd = entry.get("cwd", cwd)
                elif etype == "message":
                    msg_obj = entry.get("message") or {}
                    role = msg_obj.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    content_blocks = msg_obj.get("content") or []
                    text_parts: list[str] = []
                    if isinstance(content_blocks, str):
                        text_parts.append(content_blocks)
                    elif isinstance(content_blocks, list):
                        for blk in content_blocks:
                            if isinstance(blk, dict) and blk.get("type") == "text":
                                txt = (blk.get("text") or "").strip()
                                if txt:
                                    text_parts.append(txt)
                    full_text = "\n".join(text_parts).strip()
                    if full_text:
                        messages.append(
                            {
                                "role": role,
                                "content": full_text,
                                "timestamp": entry.get("timestamp", ""),
                            }
                        )

        return {
            "agent": "pi",
            "external_session_id": session_id,
            "cwd": cwd,
            "file_path": str(file_path),
            "messages": messages,
        }

    def read_pi_session_messages(self, pi_session_id: str) -> list[dict[str, str]]:
        fpath = self.find_session_file(pi_session_id)
        if not fpath:
            return []
        parsed = self.parse_pi_jsonl(fpath)
        return parsed["messages"]


class GoogleAgyBridge:
    """Google Antigravity (`agy`) 桥接器：支持干净拉起 `agy` 并在退出后自动捕获会话记录."""

    def __init__(
        self,
        agy_bin: Optional[str] = None,
        brain_root: str | Path = "~/.gemini/antigravity-cli/brain",
    ) -> None:
        self.agy_bin = agy_bin or shutil.which("agy") or "agy"
        self.brain_root = Path(brain_root).expanduser()

    def is_available(self) -> bool:
        return shutil.which(self.agy_bin) is not None or Path(self.agy_bin).exists()

    def build_command(
        self,
        session: TopicSession,
        prompt: Optional[str] = None,
        theme: Optional[CoreTheme] = None,
        recent_messages: Optional[list[MessageRecord]] = None,
        is_tangent: bool = False,
        interactive: bool = False,
        extra_args: Optional[list[str]] = None,
    ) -> list[str]:
        """构建拉起 `agy` 的命令行.

        核心改进：
        1. 如果用户只是运行 `mai agy`（`interactive=True` 且 `prompt=None`）：
           - 若当前话题已绑定 `agy` 的 `--conversation <id>`，直接 `agy --conversation <id>` 恢复！
           - 若当前话题还没有 `agy` 会话，且没有跨 Agent 的历史结论，直接运行纯净的 `agy`，**绝不发送任何伪造的『Google agy 专题讨论』或『拉起 agy 交互式会话』指令**！
           - 若当前话题之前在 `pi` 里已经有讨论摘要，想切到 `agy` 接着聊，才用 `-i` 传入该话题的真实历史摘要背景。
        2. 如果用户带了具体问题 `mai agy "帮我看看这段逻辑"`：
           - 若有历史脉络背景则附上，否则直接把用户问题传给 `agy`。
        """
        cmd = [self.agy_bin]
        agy_cid = session.external_sessions.get("agy")
        if agy_cid and not is_tangent:
            cmd.extend(["--conversation", agy_cid])

        if extra_args:
            cmd.extend(extra_args)

        has_ctx = has_meaningful_context(session, recent_messages)

        if interactive:
            if prompt:
                if has_ctx and not agy_cid:
                    ctx_header = build_mailuo_context_prompt(
                        session, theme=theme, recent_messages=recent_messages, is_tangent=is_tangent
                    )
                    cmd.extend(["-i", f"{ctx_header}\n\n{prompt}"])
                else:
                    cmd.extend(["-i", prompt])
            else:
                # 用户直接输入 `mai agy` 没带 prompt：
                # 只有在跨 Agent 切换（有真实历史摘要但还没有 agy conversation_id）时才注入背景
                if has_ctx and not agy_cid:
                    ctx_header = build_mailuo_context_prompt(
                        session, theme=theme, recent_messages=recent_messages, is_tangent=is_tangent
                    )
                    cmd.extend([
                        "-i",
                        f"{ctx_header}\n\nContinue from the topic context above: briefly confirm, then wait for my question.",
                    ])
                # 否则直接纯净启动 `agy`（或 `agy --conversation <id>`），不带 `-i` 假指令！
        else:
            effective_prompt = prompt or ""
            if has_ctx and not agy_cid:
                ctx_header = build_mailuo_context_prompt(
                    session, theme=theme, recent_messages=recent_messages, is_tangent=is_tangent
                )
                effective_prompt = f"{ctx_header}\n\n[Current user request]\n{effective_prompt}"
            cmd.extend(["--print", effective_prompt])

        return cmd

    def run_turn(
        self,
        session: TopicSession,
        prompt: Optional[str] = None,
        theme: Optional[CoreTheme] = None,
        recent_messages: Optional[list[MessageRecord]] = None,
        is_tangent: bool = False,
        interactive: bool = False,
        cwd: Optional[str | Path] = None,
        timeout: int = 180,
    ) -> AgentExecutionResult:
        """拉起 `agy` 并在退出后自动捕获其 `brain/<conv-id>` 下的真实对话记录回写到 Mailuo."""
        cmd = self.build_command(
            session=session,
            prompt=prompt,
            theme=theme,
            recent_messages=recent_messages,
            is_tangent=is_tangent,
            interactive=interactive,
        )
        agy_cid = session.external_sessions.get("agy")
        start_ts = time.time()

        if interactive:
            proc = run_interactive(cmd, cwd=cwd)
            captured_msgs: list[dict[str, str]] = []
            # 如果原本未绑定 agy conversation，或者刚在交互式窗口里聊了天，自动查找刚更新的 transcript.jsonl
            target_file: Optional[Path] = None
            if agy_cid:
                candidate = (
                    self.brain_root / agy_cid / ".system_generated" / "logs" / "transcript.jsonl"
                )
                if candidate.exists():
                    target_file = candidate
            else:
                recent_files = self.list_recent_transcripts(limit=3)
                for rf in recent_files:
                    if rf.stat().st_mtime >= start_ts - 2.0:
                        target_file = rf
                        break

            if target_file is not None:
                parsed = self.parse_agy_transcript(target_file)
                agy_cid = parsed["external_session_id"]
                session.external_sessions["agy"] = agy_cid
                captured_msgs = parsed["messages"]

            last_assistant = next(
                (m["content"] for m in reversed(captured_msgs) if m["role"] == "assistant"),
                "(Google Antigravity `agy` interactive session finished)",
            )
            return AgentExecutionResult(
                agent_name="agy",
                external_session_id=agy_cid,
                command=cmd,
                reply=last_assistant,
                returncode=proc.returncode,
                is_ephemeral=is_tangent,
                captured_messages=captured_msgs,
            )

        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        reply = (proc.stdout or "").strip()
        if not reply and proc.stderr:
            reply = proc.stderr.strip()
        return AgentExecutionResult(
            agent_name="agy",
            external_session_id=agy_cid,
            command=cmd,
            reply=reply,
            returncode=proc.returncode,
            is_ephemeral=is_tangent,
        )

    def list_recent_transcripts(self, limit: int = 20) -> list[Path]:
        if not self.brain_root.exists():
            return []
        files = list(self.brain_root.glob("*/.system_generated/logs/transcript.jsonl"))
        files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return files[:limit]

    @staticmethod
    def parse_agy_transcript(file_path: Path) -> dict:
        conv_id = file_path.parent.parent.parent.name
        messages: list[dict[str, str]] = []

        with file_path.open("r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    step = json.loads(line)
                except json.JSONDecodeError:
                    continue

                stype = step.get("type")
                content = (step.get("content") or "").strip()
                if not content:
                    continue

                if stype == "USER_INPUT":
                    m = re.search(
                        r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>",
                        content,
                        re.DOTALL,
                    )
                    user_text = m.group(1).strip() if m else content
                    if user_text:
                        messages.append(
                            {
                                "role": "user",
                                "content": user_text,
                                "timestamp": step.get("created_at", ""),
                            }
                        )
                elif stype == "PLANNER_RESPONSE":
                    messages.append(
                        {
                            "role": "assistant",
                            "content": content,
                            "timestamp": step.get("created_at", ""),
                        }
                    )

        return {
            "agent": "agy",
            "external_session_id": conv_id,
            "file_path": str(file_path),
            "messages": messages,
        }
