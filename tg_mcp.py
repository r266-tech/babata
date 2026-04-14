"""Standalone stdio MCP server exposing TG capabilities to CC.

Run as: python tg_mcp.py
CC CLI connects via stdio; we relay requests to the bot through Unix socket.
"""

import asyncio
import json
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

log = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/cc-tg-bridge.sock"

server = Server("tg")


def _voice_description() -> str:
    """Facts about the actual TTS backend so CC can decide how to use markup."""
    base = (
        "Synthesize text to speech and send as a TG voice message. "
        "text may include backend-specific expressive markup."
    )
    backend = os.environ.get("TTS_BACKEND", "openai").lower()
    has_custom_url = bool(os.environ.get("TTS_URL"))

    if backend == "mimo" and has_custom_url:
        return (
            f"{base} Current backend (mimo-v2-tts) recognizes "
            "<style>arbitrary natural-language description</style> prefix "
            "(emotion/dialect/role/singing/free combinations), and full-width "
            "paren inline cues like （笑）/（咳嗽）/（叹气）/（停顿） for sound events."
        )
    if has_custom_url:
        return f"{base} Current backend is OpenAI-compatible /audio/speech (plain text, no markup)."
    return f"{base} Current backend is edge-tts (plain text, no markup)."


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="tg_send_buttons",
            description=(
                "Present interactive buttons. Each option is either a label string "
                "(callback) or {label, url} (opens link). Returns the callback label "
                "clicked, or 'Links sent' if all are URL buttons."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Message above buttons"},
                    "options": {
                        "type": "array",
                        "items": {
                            "oneOf": [
                                {"type": "string"},
                                {
                                    "type": "object",
                                    "properties": {
                                        "label": {"type": "string"},
                                        "url": {"type": "string"},
                                    },
                                    "required": ["label"],
                                },
                            ]
                        },
                        "minItems": 1,
                        "maxItems": 8,
                    },
                },
                "required": ["text", "options"],
            },
        ),
        Tool(
            name="tg_send_text",
            description="Send a plain text message to the user's Telegram.",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="tg_send_file",
            description="Send a local file to the user as a TG document.",
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
            name="tg_send_album",
            description="Send 2-10 local images as a TG media album.",
            inputSchema={
                "type": "object",
                "properties": {
                    "paths": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 2,
                        "maxItems": 10,
                    },
                    "caption": {"type": "string"},
                },
                "required": ["paths"],
            },
        ),
        Tool(
            name="tg_send_location",
            description="Send a pinpoint location to the user. Attaches an Amap open-link button.",
            inputSchema={
                "type": "object",
                "properties": {
                    "latitude": {"type": "number"},
                    "longitude": {"type": "number"},
                    "name": {"type": "string", "description": "Optional place name for the map label"},
                },
                "required": ["latitude", "longitude"],
            },
        ),
        Tool(
            name="tg_send_voice",
            description=_voice_description(),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Text to speak (with optional markup)"},
                    "voice": {
                        "type": "string",
                        "description": "Optional voice identifier (backend-specific, e.g. nova/mimo_default/zh-CN-XiaoxiaoNeural)",
                    },
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="tg_send_video",
            description="Send a local video file (mp4/mov) to the user as a TG video message.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "caption": {"type": "string"},
                },
                "required": ["path"],
            },
        ),
    ]


async def _relay(action: str, **kwargs) -> str:
    """Send action to bridge, await result."""
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
        if name == "tg_send_buttons":
            result = await _relay(
                "buttons",
                text=arguments.get("text", ""),
                options=arguments.get("options", []),
            )
        elif name == "tg_send_text":
            result = await _relay("send_text", text=arguments["text"])
        elif name == "tg_send_file":
            result = await _relay(
                "send_file",
                path=arguments["path"],
                caption=arguments.get("caption", ""),
            )
        elif name == "tg_send_album":
            result = await _relay(
                "send_album",
                paths=arguments["paths"],
                caption=arguments.get("caption", ""),
            )
        elif name == "tg_send_location":
            result = await _relay(
                "send_location",
                latitude=arguments["latitude"],
                longitude=arguments["longitude"],
                name=arguments.get("name", ""),
            )
        elif name == "tg_send_voice":
            result = await _relay(
                "send_voice",
                text=arguments["text"],
                voice=arguments.get("voice", ""),
            )
        elif name == "tg_send_video":
            result = await _relay(
                "send_video",
                path=arguments["path"],
                caption=arguments.get("caption", ""),
            )
        else:
            result = f"Unknown tool: {name}"
        return [TextContent(type="text", text=result)]
    except asyncio.TimeoutError:
        return [TextContent(type="text", text="Timeout waiting for TG")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error: {e}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
