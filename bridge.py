"""Bridge between MCP tool server and TG bot via Unix socket.

The MCP server (tg_mcp.py) runs as a subprocess of CC CLI. It sends
action requests here; we execute them against the bot and return results.
Actions: buttons (waits for click), send_file / send_album / send_location (immediate).
"""

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/cc-tg-bridge.sock"


class TGBridge:
    """Unix socket server dispatching MCP actions to the TG bot."""

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

    async def _handle_connection(self, reader, writer) -> None:
        try:
            data = await asyncio.wait_for(reader.readline(), timeout=10)
            request = json.loads(data.decode())
            action = request.get("action", "buttons")

            if not self.bot or not self.chat_id:
                await self._respond(writer, "Error: no TG context")
                return

            handlers = {
                "buttons": self._handle_buttons,
                "send_text": self._handle_send_text,
                "send_file": self._handle_send_file,
                "send_album": self._handle_send_album,
                "send_location": self._handle_send_location,
                "send_voice": self._handle_send_voice,
                "send_video": self._handle_send_video,
            }
            handler = handlers.get(action)
            if not handler:
                await self._respond(writer, f"Unknown action: {action}")
                return

            await handler(request, writer)

        except Exception as e:
            log.warning("Bridge error: %s", e)
            try:
                await self._respond(writer, f"Error: {e}")
            except Exception:
                pass
        finally:
            writer.close()

    async def _respond(self, writer, result: str) -> None:
        writer.write(json.dumps({"result": result}).encode() + b"\n")
        await writer.drain()

    async def _handle_buttons(self, request, writer) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup

        text = request["text"]
        options = request["options"]

        buttons = []
        has_callback = False
        for i, opt in enumerate(options):
            if isinstance(opt, dict):
                label = opt.get("label", str(i))
                url = opt.get("url")
            else:
                label = str(opt)
                url = None
            if url:
                buttons.append(InlineKeyboardButton(label, url=url))
            else:
                buttons.append(InlineKeyboardButton(label, callback_data=f"mcp:{i}:{label[:32]}"))
                has_callback = True

        keyboard = InlineKeyboardMarkup([[b] for b in buttons])
        msg = await self.bot.send_message(
            chat_id=self.chat_id, text=text, reply_markup=keyboard,
            reply_to_message_id=self.reply_to,
        )

        if not has_callback:
            await self._respond(writer, "Links sent")
            return

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
        await self._respond(writer, choice)

    async def _handle_send_text(self, request, writer) -> None:
        await self.bot.send_message(
            chat_id=self.chat_id, text=request["text"],
            reply_to_message_id=self.reply_to,
        )
        await self._respond(writer, "Text sent")

    async def _handle_send_file(self, request, writer) -> None:
        path = Path(request["path"]).expanduser()
        if not path.exists():
            await self._respond(writer, f"Error: file not found: {path}")
            return
        caption = request.get("caption") or None
        with path.open("rb") as f:
            await self.bot.send_document(
                chat_id=self.chat_id, document=f,
                filename=path.name, caption=caption,
                reply_to_message_id=self.reply_to,
            )
        await self._respond(writer, f"Sent: {path.name}")

    async def _handle_send_album(self, request, writer) -> None:
        from telegram import InputMediaPhoto

        paths = [Path(p).expanduser() for p in request["paths"]]
        missing = [str(p) for p in paths if not p.exists()]
        if missing:
            await self._respond(writer, f"Error: not found: {missing}")
            return
        caption = request.get("caption") or None
        handles = [p.open("rb") for p in paths]
        try:
            media = [
                InputMediaPhoto(media=f, caption=caption if i == 0 else None)
                for i, f in enumerate(handles)
            ]
            await self.bot.send_media_group(
                chat_id=self.chat_id, media=media,
                reply_to_message_id=self.reply_to,
            )
        finally:
            for f in handles:
                f.close()
        await self._respond(writer, f"Sent {len(paths)} images")

    async def _handle_send_location(self, request, writer) -> None:
        from telegram import InlineKeyboardButton, InlineKeyboardMarkup
        from urllib.parse import quote

        lat = request["latitude"]
        lon = request["longitude"]
        name = request.get("name") or ""

        provider = os.environ.get("MAP_PROVIDER", "amap").lower()
        keyboard = None
        if provider == "amap":
            url = f"https://uri.amap.com/marker?position={lon},{lat}"
            if name:
                url += f"&name={quote(name)}"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f5fa\ufe0f 高德打开", url=url)],
            ])
        elif provider == "google":
            url = f"https://www.google.com/maps/search/?api=1&query={lat},{lon}"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f5fa\ufe0f Google Maps", url=url)],
            ])
        elif provider == "osm":
            url = f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=17/{lat}/{lon}"
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f5fa\ufe0f OpenStreetMap", url=url)],
            ])

        await self.bot.send_location(
            chat_id=self.chat_id,
            latitude=lat, longitude=lon,
            reply_to_message_id=self.reply_to,
            reply_markup=keyboard,
        )
        await self._respond(writer, "Location sent")

    async def _handle_send_video(self, request, writer) -> None:
        path = Path(request["path"]).expanduser()
        if not path.exists():
            await self._respond(writer, f"Error: file not found: {path}")
            return
        caption = request.get("caption") or None
        with path.open("rb") as f:
            await self.bot.send_video(
                chat_id=self.chat_id, video=f,
                filename=path.name, caption=caption,
                reply_to_message_id=self.reply_to,
            )
        await self._respond(writer, f"Video sent: {path.name}")

    async def _handle_send_voice(self, request, writer) -> None:
        from media import text_to_voice
        voice = request.get("voice") or None  # let media.py pick backend-appropriate default
        ogg = await text_to_voice(request["text"], voice=voice)
        if not ogg:
            await self._respond(writer, "Error: TTS failed")
            return
        try:
            with ogg.open("rb") as f:
                await self.bot.send_voice(
                    chat_id=self.chat_id, voice=f,
                    reply_to_message_id=self.reply_to,
                )
        finally:
            ogg.unlink(missing_ok=True)
        await self._respond(writer, "Voice sent")

    def resolve(self, msg_id: int, option_index: int, options_label: str) -> bool:
        """Called by TG callback handler when user clicks a button."""
        future = self._pending.pop(msg_id, None)
        if not future or future.done():
            return False
        future.set_result(options_label)
        return True


bridge = TGBridge()
