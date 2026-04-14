"""Thin wrapper around Claude Code SDK. One session, streaming."""

import asyncio
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
    ToolUseBlock,
)

log = logging.getLogger(__name__)

StreamCB = Callable[[str | None, dict | None, str | None], Coroutine[Any, Any, None]]

# Path to our MCP server script
_MCP_SCRIPT = str(Path(__file__).parent / "tg_mcp.py")
_VENV_PYTHON = str(Path(__file__).parent / ".venv" / "bin" / "python")


@dataclass
class Response:
    content: str
    session_id: str
    cost: float
    tools: list[str] = field(default_factory=list)


class CC:
    """Single-session Claude Code interface."""

    def __init__(self) -> None:
        self._session_id: str | None = None

    def reset(self) -> None:
        self._session_id = None

    async def query(
        self,
        prompt: str,
        images: list[dict[str, str]] | None = None,
        on_stream: StreamCB | None = None,
    ) -> Response:
        opts = ClaudeAgentOptions(
            max_turns=200,
            permission_mode="bypassPermissions",
            cwd=str(Path.home()),
            cli_path=os.environ.get("CLAUDE_CLI_PATH"),
            include_partial_messages=on_stream is not None,
            system_prompt="Source: Telegram.",
            setting_sources=["user"],  # 读 ~/.claude/{settings.json, CLAUDE.md, skills/} - 统一身份跨终端/TG/cron
            mcp_servers={
                "tg": {
                    "command": _VENV_PYTHON,
                    "args": [_MCP_SCRIPT],
                },
            },
        )

        if self._session_id:
            opts.resume = self._session_id

        try:
            return await self._run(opts, prompt, images, on_stream)
        except Exception as e:
            if self._session_id:
                log.warning("Session resume failed (%s), starting fresh", e)
                self._session_id = None
                opts.resume = None
                return await self._run(opts, prompt, images, on_stream)
            raise

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

                if on_stream and isinstance(msg, AssistantMessage):
                    for block in getattr(msg, "content", []) or []:
                        if isinstance(block, ToolUseBlock):
                            name = getattr(block, "name", "")
                            inp = getattr(block, "input", {}) or {}
                            if name and name not in tools_seen:
                                tools_seen.append(name)
                            await on_stream(name, inp, None)
                        elif isinstance(block, TextBlock):
                            await on_stream(None, None, block.text)
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

        return Response(content=content, session_id=sid or "", cost=cost, tools=tools_seen)
