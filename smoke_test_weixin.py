#!/usr/bin/env python3
"""Smoke test for weixin_ilink.py.

Verifies the protocol layer end-to-end: QR login → long-poll → echo.
Send any text to the bot from your own WeChat; the bot replies "pong: <text>".

Usage:
  .venv/bin/python smoke_test_weixin.py            # reuse cached token if present
  .venv/bin/python smoke_test_weixin.py --fresh    # force re-login

Token is cached at /tmp/babata-weixin-smoke.json (re-usable across restarts;
delete the file or pass --fresh to re-scan). This is a throwaway test cache,
not the real babata account store — weixin_bot.py will own persistence.
"""

import asyncio
import json
import logging
import sys
from pathlib import Path

from weixin_ilink import (
    ITEM_TEXT,
    WeixinClient,
    WeixinSessionExpired,
    normalize_account_id,
    start_qr_login,
    text_item,
    wait_qr_login,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("smoke")

CACHE_PATH = Path("/tmp/babata-weixin-smoke.json")


def _print_qr(url: str) -> None:
    """ASCII QR in terminal. Falls back to printing the URL if qrcode lib missing."""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        qr.print_ascii(tty=sys.stdout.isatty(), invert=True)
    except ImportError:
        print("(pip install qrcode to render ASCII QR)")
    print(f"QR URL: {url}")


async def _login_fresh() -> dict:
    log.info("requesting QR code…")
    qr = await start_qr_login()
    log.info("scan with WeChat:")
    _print_qr(qr.qrcode_url)

    def on_refresh(new_qr) -> None:
        log.info("QR refreshed, scan the new one:")
        _print_qr(new_qr.qrcode_url)

    result = await wait_qr_login(qr, on_refresh=on_refresh)
    if not result.connected:
        log.error("login failed: %s", result.message)
        sys.exit(1)

    data = {
        "account_id": normalize_account_id(result.account_id or ""),
        "raw_account_id": result.account_id,
        "bot_token": result.bot_token,
        "base_url": result.base_url,
        "user_id": result.user_id,
    }
    CACHE_PATH.write_text(json.dumps(data, indent=2))
    log.info("logged in as %s (cached to %s)", data["account_id"], CACHE_PATH)
    return data


def _load_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text())
    except Exception as e:
        log.warning("cache read failed: %s", e)
        return None
    if not all(data.get(k) for k in ("bot_token", "base_url", "account_id")):
        return None
    log.info("reusing cached account %s", data["account_id"])
    return data


async def _echo_loop(client: WeixinClient) -> None:
    get_updates_buf = ""
    fails = 0
    log.info("long-poll started; send 'hi' to your bot from WeChat (Ctrl+C to stop)")

    while True:
        try:
            resp = await client.get_updates(get_updates_buf)
        except WeixinSessionExpired as e:
            log.error("session expired: %s", e)
            log.error("rm %s and rerun to re-login", CACHE_PATH)
            return
        except Exception as e:
            fails += 1
            log.warning("getUpdates err (%d): %s", fails, e)
            if fails >= 3:
                log.warning("3 consecutive failures, backoff 30s")
                await asyncio.sleep(30)
                fails = 0
            else:
                await asyncio.sleep(2)
            continue

        fails = 0
        get_updates_buf = resp.get("get_updates_buf", get_updates_buf)

        for m in resp.get("msgs") or []:
            if m.get("message_type") != 1:  # 1 = USER (ignore our own BOT echoes)
                continue
            sender = m.get("from_user_id")
            ctx = m.get("context_token")
            for item in m.get("item_list") or []:
                itype = item.get("type")
                if itype == ITEM_TEXT:
                    text = (item.get("text_item") or {}).get("text", "")
                    log.info("← %s: %s", sender, text[:80])
                    if sender:
                        try:
                            await client.send_message(
                                sender,
                                [text_item(f"pong: {text}")],
                                context_token=ctx,
                            )
                            log.info("→ %s: pong: %s", sender, text[:40])
                        except Exception as e:
                            log.error("send failed: %s", e)
                else:
                    log.info("← %s: non-text item type=%s (ignored)", sender, itype)


async def main() -> None:
    cached = None if "--fresh" in sys.argv else _load_cache()
    data = cached or await _login_fresh()

    client = WeixinClient(
        base_url=data["base_url"],
        token=data["bot_token"],
        account_id=data["account_id"],
    )
    await _echo_loop(client)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")
