"""CC WeChat Bot — thin WeChat transport for Claude Code.

Peer of bot.py (TG). Same CC binary, same memory, same skills. Only the wire
is different — and WeChat's wire is iLink bot HTTP + CDN AES + SILK voice +
per-peer contextToken, so we need a bit more protocol work than TG.

Run:
    .venv/bin/python weixin_bot.py             # reuse stored login
    .venv/bin/python weixin_bot.py --login     # force QR re-login

Bot only does what CC physically cannot: iLink protocol, CDN crypto, SILK decode,
contextToken routing, markdown stripping for WeChat's plain-text display.
"""

import asyncio
import base64
import logging
import re
import secrets
import signal
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv(override=True)

from constants import PROJECT, STATE_DIR
from cc import CC, VENV_PYTHON
from media import transcribe_silk, understand_video
from weixin_account import (
    add_allow_from, clear_stale_for_user, get_context_token, is_allowed,
    list_account_ids, load_account, load_allow_from, load_sync_buf,
    register_account, save_account, save_sync_buf, set_context_token,
)
from weixin_bridge import bridge
from weixin_ilink import (
    ITEM_FILE, ITEM_IMAGE, ITEM_TEXT, ITEM_VIDEO, ITEM_VOICE,
    WeixinClient, WeixinSessionExpired,
    normalize_account_id, start_qr_login, text_item, wait_qr_login,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger(f"{PROJECT}.weixin")

# ── Heartbeat (双 bot 互监控, 零 LLM 成本, 镜像 bot.py 同段) ──────────
# 自己每 30s touch; 看主 TG bot 心跳, stale > 3 min 通过微信推 V (allowFrom[0]).
_HEARTBEAT_DIR = Path.home() / "cc-workspace" / "state"
_HEARTBEAT_ME = _HEARTBEAT_DIR / "babata-weixin-heartbeat"
_HEARTBEAT_PEER = _HEARTBEAT_DIR / "babata-tg-heartbeat"
_HEARTBEAT_STALE_S = 180
_HEARTBEAT_INTERVAL_S = 30


async def _heartbeat_loop(client: "WeixinClient", account_id: str) -> None:
    _HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    alerted = False
    while True:
        try:
            _HEARTBEAT_ME.touch()
            if _HEARTBEAT_PEER.exists():
                age = time.time() - _HEARTBEAT_PEER.stat().st_mtime
                if age > _HEARTBEAT_STALE_S and not alerted:
                    allow = load_allow_from(account_id)
                    target = allow[0] if allow else None
                    if target:
                        try:
                            await client.send_message(
                                target,
                                [text_item(
                                    f"⚠️ TG bot 心跳已 {int(age)}s 未更新 "
                                    f"(阈值 {_HEARTBEAT_STALE_S}s)"
                                )],
                                context_token=get_context_token(account_id, target),
                            )
                            alerted = True
                        except Exception as e:
                            log.warning("heartbeat alert send failed: %s", e)
                elif age <= 60:
                    alerted = False
        except Exception as e:
            log.warning("heartbeat loop error: %s", e)
        await asyncio.sleep(_HEARTBEAT_INTERVAL_S)

# ── CC instance (WeChat-scoped) ───────────────────────────────────────

_WEIXIN_MCP_SCRIPT = str(Path(__file__).parent / "weixin_mcp.py")

cc = CC(
    state_file=STATE_DIR / f"{PROJECT}-weixin-session.json",
    source_prompt="Source: WeChat.",
    mcp_servers={
        "weixin": {
            "command": VENV_PYTHON,
            "args": [_WEIXIN_MCP_SCRIPT],
        },
    },
)

# ── markdown → plain text (WeChat renders literal) ───────────────────

_MD_STRIPS = [
    # Strip ``` fences but KEEP the inner code (WeChat renders plaintext —
    # without the body the user just sees code disappear). The info string
    # tolerates anything except newline / backtick (covers `c#`, `.env`,
    # `shell script`, etc.); trailing newline after opener is consumed too.
    (re.compile(r"```[^\r\n`]*\r?\n?(.*?)```", re.DOTALL), r"\1"),
    (re.compile(r"`([^`]+)`"), r"\1"),
    (re.compile(r"\*\*(.+?)\*\*"), r"\1"),
    (re.compile(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)"), r"\1"),
    (re.compile(r"~~(.+?)~~"), r"\1"),
    (re.compile(r"^#{1,6}\s*", re.MULTILINE), ""),
    (re.compile(r"!\[([^\]]*)\]\([^)]+\)"), ""),
    (re.compile(r"\[([^\]]+)\]\(([^)]+)\)"), r"\1 \2"),
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), ""),
    (re.compile(r"^-{3,}$", re.MULTILINE), ""),
    (re.compile(r"^\|.*?\|$", re.MULTILINE), ""),
]


def strip_markdown(text: str) -> str:
    for pat, repl in _MD_STRIPS:
        text = pat.sub(repl, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_MAX_WX = 4000


def chunk_text(text: str, limit: int = _MAX_WX) -> list[str]:
    """Split long text at paragraph → line → sentence boundaries."""
    text = text.strip()
    if len(text) <= limit:
        return [text] if text else []
    out: list[str] = []
    while len(text) > limit:
        split = text.rfind("\n\n", 0, limit)
        if split < limit // 2:
            split = text.rfind("\n", 0, limit)
        if split < limit // 2:
            split = text.rfind("。", 0, limit)
        if split < limit // 2:
            split = text.rfind(". ", 0, limit)
        if split < limit // 2:
            split = limit
        out.append(text[:split].rstrip())
        text = text[split:].lstrip()
    if text:
        out.append(text)
    return out


# ── stream split safety ───────────────────────────────────────────────
# WeChat protocol has no edit-message; the stream coalescer must send
# multiple messages. Without boundary protection that splits between words /
# inside ``...`` / inside **...**, leaving readers with cut-off sentences and
# leftover markdown chars (because strip_markdown's pair-matching regexes
# fail when only half the pair lives in the prefix). These helpers find a
# split point that's both at a natural boundary AND keeps the prefix
# markdown-balanced; tail is returned to the buffer to merge with the next
# arriving chunk.

_BACKTICK_RE = re.compile(r"(?<!\\)`")           # unescaped backtick
_BOLD_RE = re.compile(r"(?<!\*)\*\*(?!\*)")      # ** delimiter (not *** etc.)
# Link / image opener that was not yet closed by `](url)`. If the prefix ends
# inside a link's text or url half, strip_markdown's [X](Y) regex won't match
# and the raw `[` / `(` / `]` chars leak to WeChat. We consume every complete
# link first, then check for any leftover `[`. The URL slot allows one level
# of nested parens to cover Wikipedia-style URLs like `Foo_(bar)`.
_LINK_OPENER_RE = re.compile(
    r"!?\[(?:[^\[\]]*)\]\((?:[^()]|\([^()]*\))*\)"
)


def _md_balanced(text: str) -> bool:
    """True if backtick / ** / link delimiters are all paired in `text`.

    Checks the markers strip_markdown uses character-pair regexes for:
    `code`, **bold**, [link](url), ![alt](url). Headings / lists are
    line-anchored and don't care about cross-flush splits.
    """
    if len(_BACKTICK_RE.findall(text)) % 2:
        return False
    if len(_BOLD_RE.findall(text)) % 2:
        return False
    # Link/image: any `[` (or `![`) that lacks a closed `](url)` after it
    # would leak as raw markdown when split there. Scan and remove every
    # complete `[X](Y)` / `![X](Y)`; if any unmatched `[` remains, unbalanced.
    stripped = _LINK_OPENER_RE.sub("", text)
    if "[" in stripped:
        return False
    return True


# Natural-boundary candidates ordered by preference (best → worst).
# Split point is AFTER the boundary so it stays attached to the prefix.
_SAFE_BOUNDARIES = (
    "\n\n",
    "\n",
    "。", "！", "？", "；",        # Chinese sentence terminators
    ". ", "! ", "? ", "; ",         # English sentence terminators
    "，", "、",                      # Chinese clause separators
    ", ",                            # English clause separator
    " ",                             # whitespace last resort
)


def _find_safe_split(text: str, hi: int | None = None) -> int:
    """Largest split point ≤ hi at a natural boundary that keeps text[:split]
    markdown-balanced. Returns 0 if nothing safe was found."""
    n = len(text)
    if hi is None or hi > n:
        hi = n
    if hi <= 0:
        return 0
    for boundary in _SAFE_BOUNDARIES:
        pos = text.rfind(boundary, 0, hi)
        if pos < 0:
            continue
        candidate = pos + len(boundary)
        if candidate <= 0 or candidate > hi:
            continue
        if _md_balanced(text[:candidate]):
            return candidate
    return 0


def _sanitize_unbalanced_markers(text: str) -> str:
    """Last-resort scrubber: remove individual unpaired markers from `text`
    so strip_markdown produces clean output even when we had to force-cut
    in the middle of a markdown span. Used only on the force-emit escape
    paths (cut=0 and end-of-stream drain) where holding back is worse than
    accepting char loss."""
    # Drop a trailing `**` if odd count (the unpaired one is whichever the
    # cut left orphaned; rfind is the safe choice).
    if len(_BOLD_RE.findall(text)) % 2:
        idx = text.rfind("**")
        if idx >= 0:
            text = text[:idx] + text[idx + 2:]
    # Drop a trailing unescaped backtick if odd count.
    if len(_BACKTICK_RE.findall(text)) % 2:
        matches = list(_BACKTICK_RE.finditer(text))
        if matches:
            last = matches[-1]
            text = text[:last.start()] + text[last.end():]
    # Drop trailing unmatched `[` (the link opener that never closed).
    # Iterate because Wikipedia-style URLs may need multiple passes.
    for _ in range(8):  # bounded — typical text has <2 unmatched brackets
        if _md_balanced(text):
            break
        idx = text.rfind("[")
        if idx < 0:
            break
        text = text[:idx] + text[idx + 1:]
    return text


# Stream coalescer thresholds (mirror semantics, not values, of plugin defaults).
_STREAM_FLUSH_CHARS = 1200   # try to flush once buf reaches this size
_STREAM_FLUSH_IDLE_S = 6.0   # try to flush after this much idle time
_STREAM_HARD_MAX = 3500      # force a hard cut when buf reaches this size


# ── inbound media decode ─────────────────────────────────────────────

_INBOUND_DIR = Path.home() / f".{PROJECT}" / "weixin" / "media" / "inbound"


# Per-user typing_ticket cache. Mirrors plugin's config-cache.ts: cache the
# ticket returned by getConfig for a random-up-to-24h TTL, fetch again only
# after expiry. Saves one extra HTTP call per inbound message.
_TICKET_CACHE: dict[str, tuple[str, float]] = {}  # user_id → (ticket, expires_at)
_TICKET_TTL_MAX_S = 24 * 60 * 60


async def _get_typing_ticket(
    client: WeixinClient, user_id: str, ctx_token: str | None
) -> str | None:
    import random
    cached = _TICKET_CACHE.get(user_id)
    if cached and cached[1] > time.time():
        return cached[0]
    try:
        cfg = await client.get_config(user_id, context_token=ctx_token)
        ticket = cfg.get("typing_ticket") or ""
    except Exception as e:
        log.debug("getConfig failed for %s: %s", user_id, e)
        return None
    if ticket:
        _TICKET_CACHE[user_id] = (ticket, time.time() + random.random() * _TICKET_TTL_MAX_S)
    return ticket or None


def _inbound_tmp(suffix: str) -> Path:
    _INBOUND_DIR.mkdir(parents=True, exist_ok=True)
    return _INBOUND_DIR / f"{int(time.time())}-{secrets.token_hex(6)}{suffix}"


def _sniff_image_mime(data: bytes) -> str:
    if data[:4] == b"\x89PNG":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


async def _decode_item(
    client: WeixinClient, item: dict[str, Any]
) -> tuple[str, list[dict[str, str]]]:
    """One inbound MessageItem → (text_body, images_for_cc).

    Returns text that goes into the CC prompt + base64 image blocks.
    Voice/video are converted to text descriptions; files are saved locally.
    """
    itype = item.get("type")

    if itype == ITEM_TEXT:
        return ((item.get("text_item") or {}).get("text") or "", [])

    if itype == ITEM_VOICE:
        voice = item.get("voice_item") or {}
        if voice.get("text"):  # server-provided transcription
            return (f"[语音] {voice['text']}", [])
        media = voice.get("media") or {}
        silk_path: Path | None = None
        try:
            raw = await client.download_media(media)
            silk_path = _inbound_tmp(".silk")
            silk_path.write_bytes(raw)
            text = await transcribe_silk(silk_path)
            return (f"[语音] {text}", [])
        except Exception as e:
            log.warning("voice decode failed: %s", e)
            return (f"[语音转文字失败: {e}]", [])
        finally:
            if silk_path:
                silk_path.unlink(missing_ok=True)

    if itype == ITEM_IMAGE:
        image = item.get("image_item") or {}
        media = image.get("media") or {}
        aeskey_hex = image.get("aeskey")
        try:
            raw = await client.download_media(media, aeskey_hex_override=aeskey_hex)
        except Exception as e:
            log.warning("image download failed: %s", e)
            return (f"[图片下载失败: {e}]", [])
        mime = _sniff_image_mime(raw)
        ext = "jpg" if mime == "image/jpeg" else mime.split("/")[-1]
        img_path = _inbound_tmp(f".{ext}")
        img_path.write_bytes(raw)
        return (
            f"[图片: {img_path}]",
            [{"media_type": mime,
              "data": base64.b64encode(raw).decode()}],
        )

    if itype == ITEM_VIDEO:
        video = item.get("video_item") or {}
        media = video.get("media") or {}
        video_path: Path | None = None
        try:
            raw = await client.download_media(media)
            video_path = _inbound_tmp(".mp4")
            video_path.write_bytes(raw)
            desc = await understand_video(video_path)
            return (f"[视频] {desc}" if desc else "[视频：无法理解内容]", [])
        except Exception as e:
            log.warning("video handle failed: %s", e)
            return (f"[视频处理失败: {e}]", [])
        finally:
            if video_path:
                video_path.unlink(missing_ok=True)

    if itype == ITEM_FILE:
        f = item.get("file_item") or {}
        media = f.get("media") or {}
        file_name = f.get("file_name") or "file"
        try:
            raw = await client.download_media(media)
            safe_name = re.sub(r"[/\\\x00]", "_", file_name)
            _INBOUND_DIR.mkdir(parents=True, exist_ok=True)
            path = _INBOUND_DIR / f"{int(time.time())}-{safe_name}"
            path.write_bytes(raw)
            return (f"[用户发来文件: {path}]", [])
        except Exception as e:
            log.warning("file download failed: %s", e)
            return (f"[文件下载失败: {e}]", [])

    return (f"[未知消息类型 type={itype}]", [])


def _describe_ref(ref: dict[str, Any] | None) -> str:
    if not ref:
        return ""
    title = (ref.get("title") or "").strip()
    item = ref.get("message_item") or {}
    t = item.get("type")
    if t == ITEM_TEXT:
        body = ((item.get("text_item") or {}).get("text") or "").strip()[:80]
        return f"[引用: {body}]" if body else "[引用了一条文本]"
    labels = {ITEM_IMAGE: "图片", ITEM_VOICE: "语音", ITEM_FILE: "文件", ITEM_VIDEO: "视频"}
    label = labels.get(t, "消息")
    return f"[引用 {label}: {title}]" if title else f"[引用了一条{label}]"


# ── per-user burst coalescing ────────────────────────────────────────
# WeChat has no album grouping (unlike TG's media_group_id), so image +
# caption arrive as separate msgs. Rule: image is a "comma" (wait for
# follow-up), any non-image (text/voice/video/file) is a "period" (end of
# burst). Non-image without a pending burst bypasses debounce entirely.
_MESSAGE_DEBOUNCE_S = 3.0
_pending: dict[tuple[str, str], dict[str, Any]] = {}
# Single-flight across all users/accounts: concurrent CC queries would race
# on the shared session resume (session_id written per query end).
_cc_lock = asyncio.Lock()


async def _enqueue_inbound_msg(
    client: WeixinClient, msg: dict[str, Any], account_id: str
) -> None:
    from_user = msg.get("from_user_id") or ""
    ctx_token = msg.get("context_token")

    if from_user and ctx_token:
        set_context_token(account_id, from_user, ctx_token)

    if not is_allowed(account_id, from_user):
        log.warning("ignoring unauthorized from=%s", from_user)
        return

    items = msg.get("item_list") or []
    has_image = any(item.get("type") == ITEM_IMAGE for item in items)

    key = (account_id, from_user)
    pending = _pending.get(key)

    if pending is not None:
        # Inside an active burst: always append, and if this is non-image
        # (text/voice/video/file) mark the burst complete for immediate flush.
        pending["msgs"].append(msg)
        pending["last_arrival_at"] = time.monotonic()
        if not has_image:
            pending["non_image_arrived"].set()
        return

    if not has_image:
        # No pending burst + no image — straight to CC, no debounce.
        async with _cc_lock:
            try:
                await _process_combined_msgs(client, [msg], account_id)
            except Exception:
                log.exception("msg processing crashed")
        return

    # Image starts a burst that waits for the follow-up.
    pending = {
        "msgs": [msg],
        "client": client,
        "account_id": account_id,
        "last_arrival_at": time.monotonic(),
        "non_image_arrived": asyncio.Event(),
    }
    _pending[key] = pending
    asyncio.create_task(_flush_pending(key))


async def _flush_pending(key: tuple[str, str]) -> None:
    pending = _pending.get(key)
    if pending is None:
        return
    event = pending["non_image_arrived"]
    while True:
        try:
            await asyncio.wait_for(event.wait(), timeout=_MESSAGE_DEBOUNCE_S)
            break  # non-image arrived → burst complete
        except asyncio.TimeoutError:
            pending = _pending.get(key)
            if pending is None:
                return
            if time.monotonic() - pending["last_arrival_at"] < _MESSAGE_DEBOUNCE_S:
                continue  # more images still arriving, keep waiting
            break  # true quiet window — flush what we have

    _pending.pop(key, None)
    async with _cc_lock:
        try:
            await _process_combined_msgs(
                pending["client"], pending["msgs"], pending["account_id"],
            )
        except Exception:
            log.exception("combined msg processing crashed")


async def _process_combined_msgs(
    client: WeixinClient, msgs: list[dict[str, Any]], account_id: str
) -> None:
    if not msgs:
        return

    from_user = msgs[0].get("from_user_id") or ""
    ctx_token = next(
        (m.get("context_token") for m in reversed(msgs) if m.get("context_token")),
        None,
    )
    items = [item for m in msgs for item in (m.get("item_list") or [])]

    # Decode all items (across the whole burst)
    texts: list[str] = []
    images: list[dict[str, str]] = []
    ref_note = ""
    for item in items:
        if item.get("ref_msg"):
            ref_note = _describe_ref(item.get("ref_msg"))
        text, imgs = await _decode_item(client, item)
        if text:
            texts.append(text)
        images.extend(imgs)

    combined = "\n".join(t for t in texts if t).strip()
    if ref_note:
        combined = f"{ref_note}\n{combined}" if combined else ref_note
    if not combined and not images:
        log.info("inbound from %s: no decodable content", from_user)
        return

    log.info("← %s: %s (imgs=%d, msgs=%d)", from_user, combined[:80], len(images), len(msgs))

    # Hand bridge the current conversation context so wx_mcp actions can reply
    bridge.set_context(client, from_user, ctx_token, account_id)

    # Typing on (best-effort; ticket cached across inbounds, 24h TTL)
    ticket = await _get_typing_ticket(client, from_user, ctx_token)
    if ticket:
        try:
            await client.send_typing(from_user, ticket, 1)
        except Exception as e:
            log.debug("typing on failed: %s", e)

    # Stream coalescer — wx protocol has no edit-message, so each flush sends
    # a new FINISH sendmessage. We split only at natural sentence/paragraph
    # boundaries that keep markdown pairs (`...`, **...**, [..](..)) balanced;
    # otherwise we wait. _STREAM_HARD_MAX is the upper bound past which we
    # force a cut (last whitespace within bound, retracted to nearest balanced
    # position; if even that fails, sanitize markers and emit) to avoid
    # unbounded buffering when the stream produces no boundaries.
    buf: list[str] = []
    last_flush = time.monotonic()
    flush_lock = asyncio.Lock()
    sent_any = False

    async def _flush(force: bool = False) -> None:
        """Send accumulated buf. force=True drains everything (use after stream
        ends). force=False finds a safe split, sends prefix, returns tail to
        buf. May return without sending when no safe split is reachable yet."""
        nonlocal last_flush, sent_any
        async with flush_lock:
            if not buf:
                return
            raw = "".join(buf)
            sanitize_needed = False
            if force:
                # End-of-stream drain: prefix may end mid-marker if the model
                # output was itself malformed. Run sanitize unconditionally
                # so strip_markdown sees a balanced prefix.
                prefix, tail = raw, ""
                sanitize_needed = not _md_balanced(prefix)
            else:
                split = _find_safe_split(raw)
                if split <= 0:
                    if len(raw) < _STREAM_HARD_MAX:
                        return  # wait for more chunks to give us a boundary
                    # Hard-cut fallback: last whitespace within the hard max,
                    # then retract to the nearest md-balanced position so we
                    # never leak an unpaired ` / ** / [ marker.
                    cut = raw.rfind(" ", 0, _STREAM_HARD_MAX)
                    if cut < _STREAM_HARD_MAX // 2:
                        cut = _STREAM_HARD_MAX
                    while cut > 0 and not _md_balanced(raw[:cut]):
                        cut -= 1
                    # cut == 0: the whole prefix has an unclosable marker
                    # (e.g. buf opens with a never-closed `). Rather than
                    # spin retrying forever (each new chunk re-enters here),
                    # force-emit at _STREAM_HARD_MAX // 2 with sanitize so
                    # the leaked marker is scrubbed before send. Frozen
                    # output is worse than dropping a char.
                    if cut <= 0:
                        cut = _STREAM_HARD_MAX // 2
                        sanitize_needed = True
                        log.warning(
                            "wx flush: no md-balanced prefix in %d-char buf; "
                            "force-emitting at pos %d (sanitizing markers)",
                            len(raw), cut,
                        )
                    split = cut
                prefix, tail = raw[:split], raw[split:]
            buf.clear()
            if tail:
                buf.append(tail)
            last_flush = time.monotonic()
            if sanitize_needed:
                prefix = _sanitize_unbalanced_markers(prefix)
            text = strip_markdown(prefix)
            if not text:
                return
            for chunk in chunk_text(text):
                try:
                    await client.send_message(
                        from_user, [text_item(chunk)], context_token=ctx_token,
                    )
                    sent_any = True
                except Exception as e:
                    log.error("stream send failed: %s", e)

    async def _on_stream(tool_name, tool_input, text_chunk, tool_result) -> None:
        if not text_chunk:
            return
        buf.append(text_chunk)
        cur = sum(len(b) for b in buf)
        if cur >= _STREAM_FLUSH_CHARS or (
            cur > 0 and (time.monotonic() - last_flush) >= _STREAM_FLUSH_IDLE_S
        ):
            await _flush()

    try:
        resp = await cc.query(
            combined or "[图片]", images=images or None, on_stream=_on_stream,
        )
    except Exception as e:
        log.exception("CC query failed")
        try:
            await client.send_message(
                from_user, [text_item(f"❌ 处理失败: {e}")], context_token=ctx_token,
            )
        except Exception:
            pass
        return

    # Drain any residue after CC finished — force=True bypasses safe-split
    # gating since there's no more incoming stream to wait for.
    await _flush(force=True)

    # If stream produced nothing (CC output came only via resp.content, not
    # partial chunks), send the final content as one reply.
    if not sent_any:
        final = strip_markdown(resp.content or "")
        if final:
            for chunk in chunk_text(final):
                try:
                    await client.send_message(
                        from_user, [text_item(chunk)], context_token=ctx_token,
                    )
                except Exception as e:
                    log.error("final send failed: %s", e)

    if resp.resume_note:
        try:
            await client.send_message(
                from_user, [text_item(resp.resume_note)], context_token=ctx_token,
            )
        except Exception:
            pass

    if ticket:
        try:
            await client.send_typing(from_user, ticket, 2)
        except Exception:
            pass


# ── login ────────────────────────────────────────────────────────────

def _print_qr(url: str) -> None:
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.make()
        qr.print_ascii(tty=sys.stdout.isatty(), invert=True)
    except ImportError:
        print("(install qrcode for ASCII QR: .venv/bin/pip install qrcode)")
    print(f"QR URL: {url}")


async def _interactive_login() -> str:
    log.info("requesting QR for new WeChat bot login…")
    qr = await start_qr_login()
    _print_qr(qr.qrcode_url)
    log.info("scan QR above (URL: %s)", qr.qrcode_url)

    def on_refresh(new_qr) -> None:
        log.info("QR refreshed:")
        _print_qr(new_qr.qrcode_url)
        log.info("URL: %s", new_qr.qrcode_url)

    result = await wait_qr_login(qr, on_refresh=on_refresh)
    if not result.connected:
        log.error("login failed: %s", result.message)
        sys.exit(1)

    account_id = normalize_account_id(result.account_id or "")
    if not account_id:
        log.error("login success but no accountId returned")
        sys.exit(1)

    save_account(
        account_id,
        token=result.bot_token or "",
        base_url=result.base_url or "https://ilinkai.weixin.qq.com",
        user_id=result.user_id,
    )
    register_account(account_id)
    if result.user_id:
        add_allow_from(account_id, result.user_id)
        removed = clear_stale_for_user(account_id, result.user_id)
        if removed:
            log.info("cleared %d stale accounts", len(removed))
    log.info("logged in as %s (owner=%s)", account_id, result.user_id)
    return account_id


# ── main loop ────────────────────────────────────────────────────────

async def _run_account(account_id: str) -> None:
    meta = load_account(account_id)
    if not meta:
        log.error("account %s not found in store", account_id)
        return
    client = WeixinClient(
        base_url=meta["baseUrl"],
        token=meta["token"],
        account_id=account_id,
    )
    log.info("long-poll starting for %s", account_id)

    asyncio.create_task(_heartbeat_loop(client, account_id))

    buf = load_sync_buf(account_id)
    fails = 0

    while True:
        try:
            resp = await client.get_updates(buf)
        except WeixinSessionExpired as e:
            log.error("session expired, pausing 1h: %s", e)
            await asyncio.sleep(3600)
            continue
        except Exception as e:
            fails += 1
            log.warning("getUpdates err (%d): %s", fails, e)
            if fails >= 3:
                await asyncio.sleep(30)
                fails = 0
            else:
                await asyncio.sleep(2)
            continue

        fails = 0
        new_buf = resp.get("get_updates_buf", buf)
        if new_buf != buf:
            buf = new_buf
            save_sync_buf(account_id, buf)

        for m in resp.get("msgs") or []:
            if m.get("message_type") != 1:  # USER only (ignore BOT echoes)
                continue
            try:
                await _enqueue_inbound_msg(client, m, account_id)
            except Exception:
                log.exception("msg enqueue crashed")


async def main() -> None:
    ids = list_account_ids()
    force_login = "--login" in sys.argv

    if force_login or not ids:
        account_id = await _interactive_login()
    else:
        account_id = ids[0]
        log.info("using cached account %s (use --login to add another)", account_id)

    await bridge.start()

    stop_event = asyncio.Event()

    def _on_signal() -> None:
        log.info("stop signal received…")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _on_signal)
        except NotImplementedError:
            pass

    task = asyncio.create_task(_run_account(account_id))
    try:
        await stop_event.wait()
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await bridge.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")
