"""Standalone stdio MCP server for TG buttons.

Run as: python tg_mcp.py
CC CLI connects to this via stdio transport.
Communicates with the TG bot process via a Unix socket.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

log = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/cc-tg-bridge.sock"

server = Server("tg")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="tg_send_buttons",
            description=(
                "Present interactive clickable buttons to the user in Telegram. "
                "Use when offering choices, recommendations, confirmations, or decisions. "
                "Returns the label of the option the user clicked."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Message above the buttons"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Option labels (2-8 items)",
                    },
                },
                "required": ["text", "options"],
            },
        )
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name != "tg_send_buttons":
        return [TextContent(type="text", text=f"Unknown tool: {name}")]

    text = arguments.get("text", "")
    options = arguments.get("options", [])

    if not (2 <= len(options) <= 8):
        return [TextContent(type="text", text="Error: need 2-8 options")]

    # Send request to TG bot via Unix socket, wait for response
    try:
        reader, writer = await asyncio.open_unix_connection(SOCKET_PATH)

        request = json.dumps({"text": text, "options": options})
        writer.write(request.encode() + b"\n")
        await writer.drain()

        response = await asyncio.wait_for(reader.readline(), timeout=300)
        writer.close()
        await writer.wait_closed()

        choice = json.loads(response.decode())
        return [TextContent(type="text", text=f"User selected: {choice['choice']}")]

    except asyncio.TimeoutError:
        return [TextContent(type="text", text="User did not respond in time.")]
    except Exception as e:
        return [TextContent(type="text", text=f"Error communicating with TG: {e}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
