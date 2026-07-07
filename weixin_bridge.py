"""Unix socket bridge for WeChat MCP actions.

Mirror of bridge.py (TG), but rooted in WeixinClient instead of python-telegram.
weixin_mcp.py spawns inside CC's process tree and relays actions here; we
execute them against the bot's logged-in WeixinClient (upload CDN → sendMessage)
and return results.

Actions:
    send_text / send_image / send_video / send_file / send_typing

iLink bot protocol does not support groups, buttons, location, or contact
cards, so those actions are absent (mirror TG's surface is not a goal — we
only expose what WeChat can actually deliver).
"""

import asyncio
import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/babata-weixin-bridge.sock"
_PROACTIVE_PEER_ENV = "BABATA_WEIXIN_PROACTIVE_PEER"
_PROACTIVE_MEDIA_ENV = "BABATA_WEIXIN_ALLOW_PROACTIVE_MEDIA"


class WeixinBridge:
    """Dispatch MCP actions against a logged-in WeixinClient for one peer.

    The bot calls set_context() on every inbound message so the bridge knows
    which peer (and which contextToken) to reply to. Actions arriving without
    context are rejected — e.g. a scheduled CC run trying to use wx_send_text
    from a cron without being in an active conversation has nowhere to send.
    """

    def __init__(self) -> None:
        self.client = None          # WeixinClient (set by bot)
        self.to: str | None = None  # target user_id, e.g. xxx@im.wechat
        self.context_token: str | None = None
        self.account_id: str | None = None
        self._restored_context = False
        self._server: asyncio.Server | None = None

    def set_context(
        self,
        client,
        to: str,
        context_token: str | None,
        account_id: str,
    ) -> None:
        self.client = client
        self.to = to
        self.context_token = context_token
        self.account_id = account_id
        self._restored_context = False

    async def start(self) -> None:
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        self._server = await asyncio.start_unix_server(
            self._handle_connection, path=SOCKET_PATH
        )
        os.chmod(SOCKET_PATH, 0o600)
        log.info("weixin bridge listening at %s", SOCKET_PATH)

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
            action = request.get("action", "")

            handlers = {
                "send_text": self._handle_send_text,
                "send_image": self._handle_send_image,
                "send_video": self._handle_send_video,
                "send_file": self._handle_send_file,
                "send_typing": self._handle_send_typing,
            }
            handler = handlers.get(action)
            if not handler:
                await self._respond(writer, f"Unknown action: {action or '<missing>'}")
                return

            if not self.client:
                await self._respond(writer, "Error: weixin client not logged in")
                return

            # Fall back to persistent peer when no in-memory context (e.g. cron
            # / proactive push before the user has touched the bot since boot).
            # Restoration is non-destructive: we only set self.to/context_token
            # when they are unset; an active inbound conversation keeps its
            # own peer.
            if not self.to:
                self._restore_peer_from_disk()

            if not self.to:
                await self._respond(writer, "Error: no weixin conversation context")
                return

            await handler(request, writer)
        except Exception as e:
            log.warning("weixin bridge error: %s", e)
            try:
                await self._respond(writer, f"Error: {e}")
            except Exception:
                pass
        finally:
            writer.close()

    async def _respond(self, writer, result: str) -> None:
        writer.write(json.dumps({"result": result}).encode() + b"\n")
        await writer.drain()

    def _restore_peer_from_disk(self) -> None:
        """Recover (account_id, to, context_token) from persistent state.

        Proactive sends need an explicit peer. This avoids choosing the first
        allowFrom entry after restart and sending to a stale or unintended chat.
        """
        target_peer = os.environ.get(_PROACTIVE_PEER_ENV, "").strip()
        if not target_peer:
            return
        try:
            from weixin_account import (
                list_account_ids, load_allow_from, get_context_token,
            )
        except Exception as e:
            log.debug("restore_peer: account module import failed: %s", e)
            return
        try:
            for aid in list_account_ids():
                allowed = load_allow_from(aid)
                if target_peer in allowed:
                    self.to = target_peer
                    self.context_token = get_context_token(aid, target_peer)
                    self.account_id = aid
                    self._restored_context = True
                    log.info("bridge: restored configured peer from disk account=%s peer=%s", aid, target_peer[:8] + "…")
                    return
        except Exception as e:
            log.warning("restore_peer failed: %s", e)

    async def _reject_restored_media_context(self, writer) -> bool:
        if not self._restored_context:
            return False
        if os.environ.get(_PROACTIVE_MEDIA_ENV) == "1":
            return False
        await self._respond(
            writer,
            f"Error: proactive WeChat media/file sends require fresh conversation context or {_PROACTIVE_MEDIA_ENV}=1",
        )
        return True

    # ── handlers ──────────────────────────────────────────────────────

    async def _handle_send_text(self, request, writer) -> None:
        from weixin_ilink import text_item

        text = request.get("text", "")
        if not text:
            await self._respond(writer, "Error: empty text")
            return
        await self.client.send_message(
            self.to, [text_item(text)], context_token=self.context_token,
        )
        await self._respond(writer, "Text sent")

    async def _upload_and_send(
        self,
        *,
        path: Path,
        caption: str,
        media_type: int,
        build_item,
        build_item_args: tuple = (),
    ) -> None:
        """Upload → send as two separate sendmessage calls (caption + media).

        Mirrors the official plugin's sendMediaItems: each sendmessage carries
        exactly one MessageItem. Combining caption and media into one
        item_list causes silent drop server-side.
        """
        uploaded = await self.client.upload_media(
            path.read_bytes(),
            to_user_id=self.to,
            media_type=media_type,
        )
        from weixin_ilink import text_item
        if caption:
            await self.client.send_message(
                self.to, [text_item(caption)], context_token=self.context_token,
            )
        await self.client.send_message(
            self.to,
            [build_item(uploaded, *build_item_args)],
            context_token=self.context_token,
        )

    async def _handle_send_image(self, request, writer) -> None:
        from weixin_ilink import MEDIA_IMAGE, image_item

        if await self._reject_restored_media_context(writer):
            return
        path = Path(request["path"]).expanduser()
        if not path.exists():
            await self._respond(writer, f"Error: file not found: {path}")
            return
        caption = (request.get("caption") or "").strip()
        await self._upload_and_send(
            path=path, caption=caption,
            media_type=MEDIA_IMAGE, build_item=image_item,
        )
        await self._respond(writer, f"Image sent: {path.name}")

    async def _handle_send_video(self, request, writer) -> None:
        from weixin_ilink import MEDIA_VIDEO, video_item

        if await self._reject_restored_media_context(writer):
            return
        path = Path(request["path"]).expanduser()
        if not path.exists():
            await self._respond(writer, f"Error: file not found: {path}")
            return
        caption = (request.get("caption") or "").strip()
        await self._upload_and_send(
            path=path, caption=caption,
            media_type=MEDIA_VIDEO, build_item=video_item,
        )
        await self._respond(writer, f"Video sent: {path.name}")

    async def _handle_send_file(self, request, writer) -> None:
        from weixin_ilink import MEDIA_FILE, file_item

        if await self._reject_restored_media_context(writer):
            return
        path = Path(request["path"]).expanduser()
        if not path.exists():
            await self._respond(writer, f"Error: file not found: {path}")
            return
        caption = (request.get("caption") or "").strip()
        file_name = request.get("file_name") or path.name
        await self._upload_and_send(
            path=path, caption=caption,
            media_type=MEDIA_FILE, build_item=file_item,
            build_item_args=(file_name,),
        )
        await self._respond(writer, f"File sent: {file_name}")

    async def _handle_send_typing(self, request, writer) -> None:
        status = int(request.get("status", 1))
        try:
            cfg = await self.client.get_config(self.to, context_token=self.context_token)
            ticket = cfg.get("typing_ticket")
            if not ticket:
                await self._respond(writer, "Error: no typing_ticket in getConfig response")
                return
            await self.client.send_typing(self.to, ticket, status)
            await self._respond(writer, "Typing on" if status == 1 else "Typing off")
        except Exception as e:
            await self._respond(writer, f"Error: {e}")


bridge = WeixinBridge()
