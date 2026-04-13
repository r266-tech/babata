"""Bridge between CC's MCP tools and TG bot via Unix socket.

The MCP server (tg_mcp.py) runs as a separate process spawned by CC CLI.
It sends button requests here via Unix socket. We render buttons in TG,
wait for user click, and send the choice back.
"""

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/cc-tg-bridge.sock"


class TGBridge:
    """Listens on Unix socket for MCP button requests, renders in TG."""

    def __init__(self) -> None:
        self.bot = None
        self.chat_id = None
        self.reply_to = None
        self._pending: dict[int, asyncio.Future] = {}
        self._server: asyncio.Server | None = None

    def set_context(self, bot, chat_id: int, reply_to: int | None = None) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.reply_to = reply_to

    async def start(self) -> None:
        """Start Unix socket server."""
        # Clean up stale socket
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass

        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=SOCKET_PATH
        )
        log.info("Bridge socket listening at %s", SOCKET_PATH)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        """Handle one MCP button request."""
        try:
            data = await asyncio.wait_for(reader.readline(), timeout=10)
            request = json.loads(data.decode())

            text = request["text"]
            options = request["options"]

            if not self.bot or not self.chat_id:
                writer.write(json.dumps({"choice": "Error: no TG context"}).encode() + b"\n")
                await writer.drain()
                return

            # Send buttons to TG
            from telegram import InlineKeyboardButton, InlineKeyboardMarkup

            buttons = [
                [InlineKeyboardButton(opt, callback_data=f"mcp:{i}:{opt[:32]}")]
                for i, opt in enumerate(options)
            ]
            keyboard = InlineKeyboardMarkup(buttons)

            msg = await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                reply_markup=keyboard,
                reply_to_message_id=self.reply_to,
            )

            # Wait for user click
            future: asyncio.Future[str] = asyncio.get_event_loop().create_future()
            self._pending[msg.message_id] = future

            try:
                choice = await asyncio.wait_for(future, timeout=300)
            except asyncio.TimeoutError:
                self._pending.pop(msg.message_id, None)
                choice = "timeout"
                try:
                    await msg.edit_text(f"{text}\n\n(expired)")
                except Exception:
                    pass

            writer.write(json.dumps({"choice": choice}).encode() + b"\n")
            await writer.drain()

        except Exception as e:
            log.warning("Bridge connection error: %s", e)
            try:
                writer.write(json.dumps({"choice": f"Error: {e}"}).encode() + b"\n")
                await writer.drain()
            except Exception:
                pass
        finally:
            writer.close()

    def resolve(self, msg_id: int, option_index: int, options_label: str) -> bool:
        """Called by TG callback handler when user clicks a button."""
        future = self._pending.pop(msg_id, None)
        if not future or future.done():
            return False
        future.set_result(options_label)
        return True


bridge = TGBridge()
