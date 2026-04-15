"""Standalone stdio MCP server exposing WeChat capabilities to CC.

Run as: python weixin_mcp.py
CC CLI connects via stdio; we relay requests to weixin_bot through its
Unix socket bridge (/tmp/babata-weixin-bridge.sock).

Tool surface is narrower than TG's because the iLink bot protocol does not
support buttons, locations, or contact cards — we expose only what WeChat
can actually deliver.
"""

import asyncio
import json
import logging

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

log = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/babata-weixin-bridge.sock"

server = Server("weixin")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="wx_send_text",
            description=(
                "Send a plain text message to the user's WeChat. "
                "When invoked from an inbound WeChat conversation, the assistant's "
                "final turn text is already auto-delivered — this tool is additive, "
                "for mid-turn pushes, long-running task progress, or proactive sends. "
                "WeChat ignores Markdown, so write plain text."
            ),
            inputSchema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        ),
        Tool(
            name="wx_send_image",
            description=(
                "Send a local image (jpg/png/gif/webp/bmp) to the user's WeChat. "
                "Optional caption is sent as a separate TEXT message first."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute or ~-relative file path"},
                    "caption": {"type": "string"},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="wx_send_video",
            description="Send a local video file (mp4/mov/webm) to the user's WeChat.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "caption": {"type": "string"},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="wx_send_file",
            description=(
                "Send a local file as a WeChat file attachment (any type). "
                "If file_name is omitted, the local file's basename is used."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "file_name": {"type": "string"},
                    "caption": {"type": "string"},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="wx_send_typing",
            description=(
                "Show or hide the 'typing…' indicator in WeChat. "
                "Use status=1 before a long task, status=2 to cancel early. "
                "WeChat auto-cancels typing after a short delay so repeated calls are OK."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "integer",
                        "enum": [1, 2],
                        "description": "1 = typing on, 2 = typing off",
                    },
                },
                "required": ["status"],
            },
        ),
    ]


async def _relay(action: str, **kwargs) -> str:
    reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)
    try:
        request = json.dumps({"action": action, **kwargs})
        writer.write(request.encode() + b"\n")
        await writer.drain()
        response = await asyncio.wait_for(reader.readline(), timeout=300)
        return json.loads(response.decode()).get("result", "no result")
    finally:
        writer.close()
        await writer.wait_closed()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "wx_send_text":
            result = await _relay("send_text", text=arguments["text"])
        elif name == "wx_send_image":
            result = await _relay(
                "send_image",
                path=arguments["path"],
                caption=arguments.get("caption", ""),
            )
        elif name == "wx_send_video":
            result = await _relay(
                "send_video",
                path=arguments["path"],
                caption=arguments.get("caption", ""),
            )
        elif name == "wx_send_file":
            result = await _relay(
                "send_file",
                path=arguments["path"],
                file_name=arguments.get("file_name", ""),
                caption=arguments.get("caption", ""),
            )
        elif name == "wx_send_typing":
            result = await _relay("send_typing", status=arguments["status"])
        else:
            result = f"Unknown tool: {name}"
        return [TextContent(type="text", text=result)]
    except asyncio.TimeoutError:
        return [TextContent(type="text", text="Timeout waiting for WeChat")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
