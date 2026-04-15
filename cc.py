"""Thin wrapper around Claude Code SDK. One session, streaming.

Channel-agnostic: caller passes the channel-specific state file, source prompt,
and MCP servers. This lets the TG bot and the WeChat bot share one class while
keeping session state and exposed tools isolated per channel.
"""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Coroutine

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import PermissionResultAllow, ToolPermissionContext

log = logging.getLogger(__name__)

# (tool_name, tool_input, text_chunk, tool_result) — exactly one non-None.
# tool_result = {"is_error": bool, "text": str} so bot can surface real errors
# instead of letting CC hallucinate high-level reasons ("系统限制了...").
StreamCB = Callable[
    [str | None, dict | None, str | None, dict | None],
    Coroutine[Any, Any, None],
]

VENV_PYTHON = str(Path(__file__).parent / ".venv" / "bin" / "python")

_CC_PROJECTS = Path.home() / ".claude/projects/-Users-admin"


async def _always_allow(
    tool_name: str,
    tool_input: dict[str, Any],
    ctx: ToolPermissionContext,
) -> PermissionResultAllow:
    """Auto-approve every SDK permission prompt.

    bypassPermissions mode alone doesn't cover CC's "protected paths"
    (~/.claude, .git, .ssh, .zshrc, .mcp.json, ...). Those still prompt in
    every mode except `auto` and `dontAsk`. Bot is non-interactive, so any
    prompt that reaches SDK = hung tool call.

    Personal Mac full-trust — blanket allow."""
    return PermissionResultAllow()

# Tunables. These are storage / token-budget params, not "importance judgments" —
# CC still decides meaning from the data we expose. Kept explicit so a future
# reader sees the cost model instead of magic numbers.
_MAX_RECENT_SIDS = 200          # ring buffer of past session_ids (~1y at 5/day, ~15KB state file)
_RESUME_INJECT_PAIRS = 3        # last N user+assistant pairs to inject on resume failure
_RESUME_INJECT_CHARS = 300      # per-turn char cap (3 pairs × 300 × 2 ≈ 1.8KB, fits any system_prompt)


def _extract_text(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [b.get("text", "") for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return " ".join(parts).strip()
    return ""


def _tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, dict):
                t = b.get("text") or b.get("content")
                if t:
                    parts.append(str(t))
            else:
                parts.append(str(b))
        return "".join(parts)
    return str(content)


@dataclass
class Response:
    content: str
    session_id: str
    cost: float
    tools: list[str] = field(default_factory=list)
    resume_note: str | None = None  # populated when SDK resume failed + we recovered


class CC:
    """Single-session Claude Code interface for one channel.

    Each channel (TG / WeChat) owns its own CC instance: separate state file,
    separate resume history, separate MCP tool surface.
    """

    def __init__(
        self,
        *,
        state_file: Path,
        source_prompt: str,
        mcp_servers: dict[str, Any] | None = None,
    ) -> None:
        self._state_file = state_file
        self._source_prompt = source_prompt
        self._mcp_servers = mcp_servers or {}
        self._session_id: str | None = self._load_state().get("session_id")

    # ── state persistence (per-channel) ──────────────────────────────

    def _load_state(self) -> dict:
        try:
            return json.loads(self._state_file.read_text())
        except Exception:
            return {}

    def _save_state(self, state: dict) -> None:
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(state))
        except Exception as e:
            log.warning("Failed to persist session state: %s", e)

    def _record_sid(self, sid: str | None) -> None:
        state = self._load_state()
        state["session_id"] = sid
        if sid:
            hist = [s for s in state.get("recent_sids", []) if s != sid]
            hist.insert(0, sid)
            state["recent_sids"] = hist[:_MAX_RECENT_SIDS]
        self._save_state(state)

    def _recent_turns_summary(self) -> str:
        """Take the most recent session (tracked in state.recent_sids) and
        extract last _RESUME_INJECT_PAIRS user+assistant pairs. Returns '' if
        state empty or no session file usable."""
        sids = self._load_state().get("recent_sids") or []
        for sid in sids:
            target = _CC_PROJECTS / f"{sid}.jsonl"
            if not target.is_file():
                continue
            turns: list[tuple[str, str]] = []
            try:
                for line in target.read_text().splitlines():
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    msg = d.get("message")
                    if not isinstance(msg, dict):
                        continue
                    role = msg.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    text = _extract_text(msg.get("content"))
                    if text:
                        turns.append((role, text))
            except Exception:
                continue
            if not turns:
                continue
            recent = turns[-(2 * _RESUME_INJECT_PAIRS):]
            lines = [f"{'V' if r == 'user' else 'CC'}: {t[:_RESUME_INJECT_CHARS]}"
                     for r, t in recent]
            return "会话从历史归档恢复, 最近几轮:\n" + "\n".join(lines)
        return ""

    def reset(self) -> None:
        self._session_id = None
        self._record_sid(None)

    # ── query ────────────────────────────────────────────────────────

    async def query(
        self,
        prompt: str,
        images: list[dict[str, str]] | None = None,
        on_stream: StreamCB | None = None,
    ) -> Response:
        opts = ClaudeAgentOptions(
            max_turns=200,
            permission_mode="bypassPermissions",
            can_use_tool=_always_allow,  # auto-approve protected-path prompts that bypassPermissions still forwards
            cwd=str(Path.home()),
            cli_path=os.environ.get("CLAUDE_CLI_PATH"),
            include_partial_messages=on_stream is not None,
            system_prompt=self._source_prompt,
            setting_sources=["user"],  # 读 ~/.claude/{settings.json, CLAUDE.md, skills/} - 统一身份跨终端/TG/cron
            mcp_servers=self._mcp_servers,
        )

        if self._session_id:
            opts.resume = self._session_id

        try:
            return await self._run(opts, prompt, images, on_stream)
        except Exception as e:
            if not self._session_id:
                raise
            log.warning("Session resume failed (%s), injecting recent history", e)
            self._session_id = None
            self._record_sid(None)
            opts.resume = None
            ctx = self._recent_turns_summary()
            if ctx:
                opts.system_prompt = f"{self._source_prompt}\n\n{ctx}"
                note = f"⚠️ 会话重置 ({type(e).__name__}), 已从归档注入最近 {_RESUME_INJECT_PAIRS} 轮"
            else:
                note = f"⚠️ 会话重置 ({type(e).__name__}), 历史归档也没找到"
            resp = await self._run(opts, prompt, images, on_stream)
            resp.resume_note = note
            return resp

    async def _run(
        self,
        opts: ClaudeAgentOptions,
        prompt: str,
        images: list[dict[str, str]] | None,
        on_stream: StreamCB | None,
    ) -> Response:
        client = ClaudeSDKClient(opts)
        messages = []
        tools_seen: list[str] = []

        try:
            await client.connect()

            if images:
                blocks: list[dict[str, Any]] = [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": img["media_type"],
                            "data": img["data"],
                        },
                    }
                    for img in images
                ]
                if prompt:
                    blocks.append({"type": "text", "text": prompt})

                async def _multi():
                    yield {"type": "user", "message": {"role": "user", "content": blocks}}

                await client.query(_multi())
            else:
                await client.query(prompt)

            async for msg in client.receive_messages():
                messages.append(msg)

                if isinstance(msg, ResultMessage):
                    break

                if not on_stream:
                    continue

                if isinstance(msg, AssistantMessage):
                    for block in getattr(msg, "content", []) or []:
                        if isinstance(block, ToolUseBlock):
                            name = getattr(block, "name", "")
                            inp = getattr(block, "input", {}) or {}
                            if name and name not in tools_seen:
                                tools_seen.append(name)
                            await on_stream(name, inp, None, None)
                        elif isinstance(block, TextBlock):
                            await on_stream(None, None, block.text, None)
                elif isinstance(msg, UserMessage):
                    for block in getattr(msg, "content", []) or []:
                        if isinstance(block, ToolResultBlock):
                            await on_stream(None, None, None, {
                                "is_error": bool(block.is_error),
                                "text": _tool_result_text(block.content),
                            })
        finally:
            await client.disconnect()

        content = ""
        cost = 0.0
        sid = None

        for msg in messages:
            if isinstance(msg, ResultMessage):
                cost = getattr(msg, "total_cost_usd", 0.0) or 0.0
                sid = getattr(msg, "session_id", None)
                result = getattr(msg, "result", None)
                if result:
                    content = str(result).strip()
                break

        if not content:
            parts = []
            for msg in messages:
                if isinstance(msg, AssistantMessage):
                    for block in getattr(msg, "content", []) or []:
                        if hasattr(block, "text"):
                            parts.append(block.text)
            content = "\n".join(parts).strip()

        if not content and tools_seen:
            content = f"Done. Tools used: {', '.join(tools_seen)}"

        if sid:
            self._session_id = sid
            self._record_sid(sid)

        return Response(content=content, session_id=sid or "", cost=cost, tools=tools_seen)
