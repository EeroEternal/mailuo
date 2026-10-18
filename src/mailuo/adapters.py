"""Multi-Agent Connectors & Transcript Syncers for pi.dev (`pi`) and Google Antigravity (`agy`).

支持两种核心模式：
1. **拉起外部 Agent 并按『脉络 TopicSession』自动托管记录 (`spawn_or_ask`)**：
   - 针对 `pi` (pi.dev)：
     * 当开启或切换到某个 `TopicSession` 时，自动为其分配或恢复专属的 `--session-id <uuid>` 和 `--name <话题标题>`，并通过 `--append-system-prompt` 注入 Mailuo 提炼的结构化上下文摘要。
     * 当用户在当前话题下**穿插临时问题（Side-Tangent）**时，自动带上 `--no-session` 拉起 `pi`，从物理层面保证 `pi` 的原生 Session 绝不被杂事污染！
   - 针对 `agy` (Google Antigravity CLI)：
     * 自动关联 `--conversation <id>` 或通过 `--print` / 交互式拉起，并将 Mailuo 中跨 Agent（比如刚跟 `pi` 聊出的结论）的上下文摘要同步注入给 `agy`。
2. **解析并导入 `pi` 与 `agy` 的本地原生 JSONL 会话日志 (`sync_from_disk`)**：
   - 自动扫描 `~/.pi/agent/sessions/**/*.jsonl` 和 `~/.gemini/antigravity-cli/brain/*/transcript.jsonl`
   - 按照 Mailuo 的逻辑拆解、提炼结构化摘要，并支持跨 `pi` 和 `agy` 统一检索与整理核心主题。
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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


def build_mailuo_context_prompt(
    session: TopicSession,
    theme: Optional[CoreTheme] = None,
    recent_messages: Optional[list[MessageRecord]] = None,
    is_tangent: bool = False,
) -> str:
    """构建跨 Agent 共享的脉络上下文提示词（让 pi 和 agy 共享同一个具体话题的记忆）."""
    lines = [
        f"【Mailuo 脉络上下文】当前聚焦具体话题：《{session.title}》 (ID: {session.id})",
    ]
    if theme:
        lines.append(
            f"【所属核心主题】《{theme.name}》— 长期核心共识：{theme.consolidated_insight}"
        )
    if session.summary:
        lines.append(f"【话题已沉淀摘要】{session.summary}")
    if session.key_decisions:
        lines.append("【已达成关键结论】" + "；".join(session.key_decisions))
    if session.open_questions:
        lines.append("【待推进问题】" + "；".join(session.open_questions))

    # 如果最近有其他 Agent (比如从 pi 切到 agy，或从 agy 切到 pi) 的对话，附上精简记录
    if recent_messages:
        cross_notes = []
        for m in recent_messages[-4:]:
            role_label = "用户" if m.role == "user" else f"Agent({m.agent_source})"
            snippet = m.content.strip().replace("\n", " ")[:160]
            cross_notes.append(f"- {role_label}: {snippet}")
        if cross_notes:
            lines.append("【近期脉络对话片段】\n" + "\n".join(cross_notes))

    if is_tangent:
        lines.append(
            "【重要提示】本次提问为『临时穿插提问 (Side-Tangent)』，请直接简明作答，不要偏离或改写主话题结论。"
        )
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

        - 如果是正常话题推进：绑定 `--session-id <uuid>` 和 `--name <话题标题>`，让 pi 原生持久化该具体话题。
        - 如果是临时穿插问题 (`is_tangent=True`)：自动附加 `--no-session`！这样在 pi 里问杂事完全不会污染主话题的 pi session！
        """
        context_prompt = build_mailuo_context_prompt(
            session, theme=theme, recent_messages=recent_messages, is_tangent=is_tangent
        )
        cmd = [self.pi_bin]

        if is_tangent:
            cmd.append("--no-session")
        else:
            pi_sid = self.ensure_pi_session_id(session)
            cmd.extend(["--session-id", pi_sid, "--name", f"[脉络] {session.title}"])

        cmd.extend(["--append-system-prompt", context_prompt])

        if extra_args:
            cmd.extend(extra_args)

        if not interactive and prompt:
            cmd.extend(["--print", prompt])
        elif prompt:
            cmd.append(prompt)

        return cmd

    def run_turn(
        self,
        session: TopicSession,
        prompt: str,
        theme: Optional[CoreTheme] = None,
        recent_messages: Optional[list[MessageRecord]] = None,
        is_tangent: bool = False,
        interactive: bool = False,
        cwd: Optional[str | Path] = None,
        timeout: int = 180,
    ) -> AgentExecutionResult:
        """拉起 `pi` 执行对话，并自动提取最新回复以记录回 Mailuo."""
        cmd = self.build_command(
            session=session,
            prompt=prompt,
            theme=theme,
            recent_messages=recent_messages,
            is_tangent=is_tangent,
            interactive=interactive,
        )
        pi_sid = session.external_sessions.get("pi")

        if interactive:
            # 交互式直接接管终端 TUI，退出后自动从 ~/.pi/agent/sessions 增量读取刚聊的内容
            proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
            synced_msgs = self.read_pi_session_messages(pi_sid) if pi_sid else []
            last_assistant = next(
                (m["content"] for m in reversed(synced_msgs) if m["role"] == "assistant"),
                "（已完成 pi 交互式会话并同步脉络记录）",
            )
            return AgentExecutionResult(
                agent_name="pi",
                external_session_id=pi_sid,
                command=cmd,
                reply=last_assistant,
                returncode=proc.returncode,
                is_ephemeral=is_tangent,
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
        """在 ~/.pi/agent/sessions 下查找指定 UUID 的 .jsonl 会话文件."""
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
        """解析 pi.dev 的单条会话 JSONL 文件，提取 session_id、cwd 以及纯文本对话列表."""
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
    """Google Antigravity (`agy`) 桥接器：支持拉起 `agy` 以及解析 `~/.gemini/antigravity-cli/brain/*/transcript.jsonl`."""

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
        prompt: str,
        theme: Optional[CoreTheme] = None,
        recent_messages: Optional[list[MessageRecord]] = None,
        is_tangent: bool = False,
        interactive: bool = False,
        extra_args: Optional[list[str]] = None,
    ) -> list[str]:
        """构建拉起 `agy` 的命令行.

        - 若该 TopicSession 已绑定 `agy` 的 conversation ID 且不是穿插提问，则带上 `--conversation <id>` 恢复对话。
        - 同时把 Mailuo 的跨 Agent 脉络摘要组装进 Prompt，使得你在 `pi` 里讨论出的结论能直接被 `agy` 感知！
        """
        context_header = build_mailuo_context_prompt(
            session, theme=theme, recent_messages=recent_messages, is_tangent=is_tangent
        )
        enriched_prompt = f"{context_header}\n\n【用户当前请求】\n{prompt}"

        cmd = [self.agy_bin]
        agy_cid = session.external_sessions.get("agy")
        if agy_cid and not is_tangent:
            cmd.extend(["--conversation", agy_cid])

        if extra_args:
            cmd.extend(extra_args)

        if interactive:
            cmd.extend(["-i", enriched_prompt])
        else:
            cmd.extend(["--print", enriched_prompt])

        return cmd

    def run_turn(
        self,
        session: TopicSession,
        prompt: str,
        theme: Optional[CoreTheme] = None,
        recent_messages: Optional[list[MessageRecord]] = None,
        is_tangent: bool = False,
        interactive: bool = False,
        cwd: Optional[str | Path] = None,
        timeout: int = 180,
    ) -> AgentExecutionResult:
        """拉起 `agy` 并记录输出回 Mailuo."""
        cmd = self.build_command(
            session=session,
            prompt=prompt,
            theme=theme,
            recent_messages=recent_messages,
            is_tangent=is_tangent,
            interactive=interactive,
        )
        agy_cid = session.external_sessions.get("agy")

        if interactive:
            proc = subprocess.run(cmd, cwd=str(cwd) if cwd else None)
            return AgentExecutionResult(
                agent_name="agy",
                external_session_id=agy_cid,
                command=cmd,
                reply="（已完成 Google Antigravity `agy` 交互式会话）",
                returncode=proc.returncode,
                is_ephemeral=is_tangent,
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
        """解析 Google Antigravity (`agy`) 的 transcript.jsonl，提取 conversation_id 和清洗后的对话."""
        # 路径形如 ~/.gemini/antigravity-cli/brain/<conversation-id>/.system_generated/logs/transcript.jsonl
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
                    # 提取 <USER_REQUEST>...</USER_REQUEST> 内部纯净文本
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
