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
import fcntl
import json
import logging
import os
import re
import secrets
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True

from dotenv import load_dotenv

# Explicit service configuration wins; shared .env only supplies defaults.
load_dotenv(override=False)

from constants import PROJECT, STATE_DIR, WEIXIN_DATA_DIR
from engine import VENV_PYTHON, make_engine
from media import transcribe_silk, transcribe_voice, understand_video
from weixin_account import (
    add_allow_from, clear_stale_for_user, get_context_token, is_allowed,
    list_account_ids, load_account, load_allow_from, load_sync_buf,
    recall_message, register_account, remember_message, reply_quota_path,
    save_account, save_sync_buf, set_context_token,
)
from weixin_bridge import bridge
from weixin_ilink import (
    ITEM_FILE, ITEM_IMAGE, ITEM_TEXT, ITEM_VIDEO, ITEM_VOICE,
    WeixinClient, WeixinQuotaExhausted, WeixinSessionExpired,
    normalize_account_id, send_image, start_qr_login, text_item, wait_qr_login,
    wechat_markdown,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger(f"{PROJECT}.weixin")

# ── Heartbeat (双 bot 互监控, 零 LLM 成本, 镜像 bot.py 同段) ──────────
# 自己每 30s touch; 看主 TG bot 心跳, stale > 3 min 通过微信推 V (allowFrom[0]).
_HEARTBEAT_ME = STATE_DIR / f"{PROJECT}-weixin-heartbeat"
_HEARTBEAT_PEER = STATE_DIR / f"{PROJECT}-tg-heartbeat"
_HEARTBEAT_STALE_S = 180
_HEARTBEAT_INTERVAL_S = 30

PENDING_WX_UPDATES_FILE = STATE_DIR / f"{PROJECT}-weixin-pending-updates.json"


async def _heartbeat_loop(client: "WeixinClient", account_id: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    alerted = False
    peer_alive = False  # a peer that was never alive this run is retired, not down
    while True:
        try:
            _HEARTBEAT_ME.touch()
            if _HEARTBEAT_PEER.exists():
                age = time.time() - _HEARTBEAT_PEER.stat().st_mtime
                peer_alive = peer_alive or age <= _HEARTBEAT_STALE_S
                if age > _HEARTBEAT_STALE_S and peer_alive and not alerted:
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

_WX_SOURCE_PROMPT = (
    "Source: WeChat. "
    "Renders Markdown: # to ####, **bold**, lists, > quote, tables, `code`, fences; "
    "no italics on Chinese. "
    "bare URLs visible, [text](url) hides URL. "
    "![](local path or https URL) arrives as a real image. "
    "New bubble: \\n\\n\\n. "
    "No edit-message; "
    "Max 4000."
)

_WX_MCP_SERVERS = {
    "weixin": {
        "command": VENV_PYTHON,
        "args": [_WEIXIN_MCP_SCRIPT],
    },
}

# Same CPU vocabulary as bin/babata-cpu: BABATA_PRIMARY_CPU answers (Claude
# tuned by BABATA_CLAUDE_{MODEL,EFFORT}); when it reports a usage limit or auth
# failure the turn goes to BABATA_FALLBACK_CPU (Codex tuned by BABATA_CODEX_*).
# Each CPU keeps its own state file so session ids never cross CPUs.
_WX_ENGINE = os.environ.get("BABATA_PRIMARY_CPU") or None
_WX_FALLBACK_ENGINE = os.environ.get("BABATA_FALLBACK_CPU") or None


def _wx_state_file(engine: str | None) -> Path:
    suffix = f"-{engine}" if engine else ""
    return STATE_DIR / f"{PROJECT}-weixin{suffix}-session.json"


cc = make_engine(
    state_file=_wx_state_file(_WX_ENGINE),
    source_prompt=_WX_SOURCE_PROMPT,
    memory_source="wechat",
    mcp_servers=_WX_MCP_SERVERS,
    engine=_WX_ENGINE,
    model=os.environ.get("BABATA_CLAUDE_MODEL") or None,
    effort=os.environ.get("BABATA_CLAUDE_EFFORT") or None,
)
fallback_cc = make_engine(
    state_file=_wx_state_file(_WX_FALLBACK_ENGINE),
    source_prompt=_WX_SOURCE_PROMPT,
    memory_source="wechat",
    mcp_servers=_WX_MCP_SERVERS,
    engine=_WX_FALLBACK_ENGINE,
) if _WX_FALLBACK_ENGINE else None

# ── markdown handling ────────────────────────────────────────────────

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


def _strip_markdown(text: str) -> str:
    """WX client renders markdown natively (2026-04-30 实测两轮截图证实).
    Strip 默认 disabled — markdown 直传 wire, 客户端渲染. 仅 collapse 多空行/边界 trim.
    Set BABATA_WEIXIN_STRIP_MD=1 兜底 (老客户端不渲染时 opt-in)."""
    if os.environ.get("BABATA_WEIXIN_STRIP_MD") == "1":
        for pat, repl in _MD_STRIPS:
            text = pat.sub(repl, text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


_MAX_WX = 4000
_WX_INPUT_MAX_CHARS = int(os.environ.get("BABATA_WX_INPUT_MAX_CHARS", "12000"))
_WX_INPUT_TRUNCATED_NOTE = "\n\n[WeChat input truncated]"


# ── stream split safety ───────────────────────────────────────────────
# WeChat has no edit-message; every streamed flush is final. Even when the
# client renders Markdown natively, splitting inside `...`, **...**, or
# [label](url) leaves half-markers visible. Keep split points natural and
# Markdown-balanced; hard-cut paths sanitize the prefix before sending.
_BACKTICK_RE = re.compile(r"(?<!\\)`")           # unescaped backtick
_BOLD_RE = re.compile(r"(?<!\*)\*\*(?!\*)")      # ** delimiter (not *** etc.)
_SAFE_BOUNDARIES = (
    "\n\n",
    "\n",
    "。", "！", "？", "；",
    ". ", "! ", "? ", "; ",
    "，", "、",
    ", ",
    " ",
)


def _first_unbalanced_link_start(text: str) -> int | None:
    i = 0
    n = len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        image = text[i] == "!" and i + 1 < n and text[i + 1] == "["
        if image or text[i] == "[":
            start = i + 1 if image else i
            close = text.find("]", start + 1)
            if close < 0:
                return start
            if close + 1 < n and text[close + 1] == "(":
                depth = 0
                j = close + 1
                while j < n:
                    if text[j] == "\\":
                        j += 2
                        continue
                    if text[j] == "(":
                        depth += 1
                    elif text[j] == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    j += 1
                if depth != 0:
                    return start
                i = j + 1
                continue
            i = close + 1
            continue
        i += 1
    return None


def _md_balanced(text: str) -> bool:
    """True if unambiguous Markdown delimiters are paired in `text`."""
    if len(_BACKTICK_RE.findall(text)) % 2:
        return False
    if len(_BOLD_RE.findall(text)) % 2:
        return False
    return _first_unbalanced_link_start(text) is None


def _find_safe_split(
    text: str,
    hi: int | None = None,
    boundaries: tuple[str, ...] = _SAFE_BOUNDARIES,
) -> int:
    """Largest split point <= hi at a natural Markdown-balanced boundary."""
    n = len(text)
    if hi is None or hi > n:
        hi = n
    if hi <= 0:
        return 0
    for boundary in boundaries:
        search_hi = hi
        while search_hi > 0:
            pos = text.rfind(boundary, 0, search_hi)
            if pos < 0:
                break
            candidate = pos + len(boundary)
            if 0 < candidate <= hi and _md_balanced(text[:candidate]):
                return candidate
            search_hi = pos
    return 0


def _sanitize_unbalanced_markers(text: str) -> str:
    """Last-resort scrubber for force-cut chunks with unmatched markers."""
    if len(_BOLD_RE.findall(text)) % 2:
        idx = text.rfind("**")
        if idx >= 0:
            text = text[:idx] + text[idx + 2:]
    if len(_BACKTICK_RE.findall(text)) % 2:
        matches = list(_BACKTICK_RE.finditer(text))
        if matches:
            last = matches[-1]
            text = text[:last.start()] + text[last.end():]
    link_start = _first_unbalanced_link_start(text)
    if link_start is not None:
        start = link_start - 1 if link_start > 0 and text[link_start - 1] == "!" else link_start
        text = text[:start]
    return text


def _chunk_text(text: str, limit: int = _MAX_WX) -> list[str]:
    """Hard-cap chunker — bubbles are pre-split by LLM via \\n\\n\\n; this
    only fires when one bubble exceeds `limit` (rare: LLM verbose without
    breaking). Prefers balanced natural boundaries; hard-cuts as last resort."""
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    while len(text) > limit:
        cut = _find_safe_split(text, limit)
        if cut <= 0:
            cut = limit
            chunk = _sanitize_unbalanced_markers(text[:cut])
        else:
            chunk = text[:cut]
        out.append(chunk.rstrip())
        text = text[cut:].lstrip()
    if text:
        out.append(text)
    return out


@dataclass(frozen=True)
class WxBubbleSendResult:
    sent_any: bool
    ok: bool


async def _send_wx_image(
    client: WeixinClient,
    to_user: str,
    ctx_token: str | None,
    target: str | Path,
) -> None:
    await send_image(client, to_user, str(target), context_token=ctx_token)


async def _send_wx_bubble(
    client: WeixinClient,
    from_user: str,
    ctx_token: str | None,
    text: str,
    account_id: str = "",
) -> WxBubbleSendResult:
    """Send one logical WeChat bubble, with markdown stripping, chunking, and one retry.

    Markdown images are pulled out of the text and delivered as image messages.
    """
    text, image_targets = wechat_markdown(text)
    text = _strip_markdown(text)
    if not text and not image_targets:
        return WxBubbleSendResult(sent_any=False, ok=True)
    sent_any = False
    ok = True
    for chunk in _chunk_text(text):
        success = False
        for attempt in range(2):
            try:
                resp = await client.send_message(
                    from_user, [text_item(chunk)], context_token=ctx_token,
                )
                remember_message(account_id, (resp or {}).get("message_id"), chunk)
                sent_any = True
                success = True
                break
            except WeixinQuotaExhausted:  # queued for the user's next message: handled, never retried
                sent_any = True
                success = True
                break
            except Exception:
                if attempt == 0:
                    log.warning("wx send failed (retry once)", exc_info=True)
                    await asyncio.sleep(0.5)
                else:
                    log.error("wx send failed (gave up)", exc_info=True)
        if not success:
            ok = False
    for target in image_targets:
        try:
            await _send_wx_image(client, from_user, ctx_token, target)
            sent_any = True
        except WeixinQuotaExhausted:
            sent_any = True
        except Exception as e:
            log.warning("markdown image delivery failed: %s: %s", target, e)
            try:
                await client.send_message(
                    from_user, [text_item("[图片发送失败]")],
                    context_token=ctx_token,
                )
                sent_any = True
            except Exception:
                ok = False
    return WxBubbleSendResult(sent_any=sent_any, ok=ok)


# ── stream coalescer thresholds ───────────────────────────────────────
# LLM marks bubble boundaries with \n\n\n (see _WX_SOURCE_PROMPT). _drain
# splits buf on that marker. Below are safety nets for when the LLM hangs
# mid-bubble or forgets to mark — both still split at safe boundaries
# (paragraph / line / sentence-end), never mid-word, mid-token, or mid-marker.
_STREAM_FLUSH_IDLE_S = 3.0   # idle drain — ship completed boundary or hold
_STREAM_HARD_MAX = 3500      # bubble exceeded this — force ship at safest split
_MERGE_WHEN_LEFT = 3         # quota sends left at which the rest of a turn becomes one bubble


# Boundary preference for splitting trailing partials, longest-context first.
# CJK terminators don't need trailing space; ASCII ones do (avoids cutting
# "v1.0" / "Mr.X"). Bullet markers ("- " / "* ") aren't safe — splitting
# there would orphan them onto the next bubble as raw chars.
_PARTIAL_BOUNDARIES_NEWLINE = ("\n\n", "\n")
_PARTIAL_BOUNDARIES_CJK = ("。", "！", "？", "；", "…")
_PARTIAL_BOUNDARIES_ASCII = (". ", "! ", "? ", "; ")


def _safe_split_partial(text: str) -> tuple[str, str]:
    """Split a trailing partial at the latest safe boundary.

    Returns (head, hold). head is shippable; hold is the leftover that
    callers append back to buf. Empty head means no boundary was found —
    callers must decide between holding all of `text` or hard-cutting.

    Boundary preference: \\n\\n > \\n > CJK terminator > ASCII terminator+space.
    """
    if not text:
        return "", ""
    split = _find_safe_split(
        text,
        boundaries=(
            _PARTIAL_BOUNDARIES_NEWLINE
            + _PARTIAL_BOUNDARIES_CJK
            + _PARTIAL_BOUNDARIES_ASCII
        ),
    )
    if split > 0:
        return text[:split], text[split:]
    return "", text


class WxStreamCoalescer:
    def __init__(self, send_bubble: Any, quota_left: Any = None) -> None:
        self._send_bubble = send_bubble
        # Few sends left in the user's quota: hold the rest of the turn and ship it as one bubble.
        self._quota_left = quota_left or (lambda: None)
        self._buf: list[str] = []
        self._last_flush: float | None = None
        self._flush_lock = asyncio.Lock()
        self.sent_any = False
        self.had_send_failure = False

    def reset_for_replay(self) -> None:
        self._buf.clear()
        self._last_flush = None
        self.sent_any = False
        self.had_send_failure = False

    async def send_bubble(self, text: str) -> bool:
        result = await self._send_bubble(text)
        self.sent_any = self.sent_any or result.sent_any
        self.had_send_failure = self.had_send_failure or not result.ok
        return result.ok

    def _merging(self) -> bool:
        left = self._quota_left()
        return left is not None and left <= _MERGE_WHEN_LEFT

    async def drain(self, allow_hard_cut: bool) -> None:
        async with self._flush_lock:
            if self._merging():
                if allow_hard_cut and self._buf:
                    raw = "".join(self._buf)
                    self._buf.clear()
                    await self.send_bubble(re.sub(r"\n{3,}", "\n\n", raw))
                return
            while True:
                if not self._buf:
                    break
                raw = "".join(self._buf)
                self._buf.clear()
                parts = re.split(r"\n{3,}", raw)
                tail = parts.pop()
                for part in parts:
                    await self.send_bubble(part)
                if not tail:
                    break
                head, hold = _safe_split_partial(tail)
                if head:
                    await self.send_bubble(head)
                    if hold:
                        self._buf.append(hold)
                        if allow_hard_cut:
                            continue
                    break
                if allow_hard_cut:
                    await self.send_bubble(tail)
                    break
                self._buf.append(tail)
                break
            self._last_flush = time.monotonic()

    async def on_stream(
        self,
        _tool_name: Any,
        _tool_input: Any,
        text_chunk: str | None,
        _tool_result: Any,
    ) -> None:
        if not text_chunk:
            return
        if self._last_flush is None:
            self._last_flush = time.monotonic()
        self._buf.append(text_chunk)
        if self._merging():
            return  # the end-of-stream drain ships it all as one bubble
        raw = "".join(self._buf)
        if "\n\n\n" in raw:
            await self.drain(allow_hard_cut=False)
        elif len(raw) >= _STREAM_HARD_MAX:
            await self.drain(allow_hard_cut=True)
        elif time.monotonic() - self._last_flush >= _STREAM_FLUSH_IDLE_S:
            await self.drain(allow_hard_cut=False)


# ── inbound media decode ─────────────────────────────────────────────

_INBOUND_DIR = WEIXIN_DATA_DIR / "media" / "inbound"


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


# Claude rejects inline images above ~5 MB base64; larger ones are shrunk with
# macOS sips first, and fall back to path-only when that is not enough.
_INLINE_IMAGE_MAX = 3_750_000
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tif", ".tiff"}
# VoiceItem.encode_type → file suffix ffmpeg can read; None / 6 are SILK.
_VOICE_SUFFIXES = {1: ".pcm", 2: ".adpcm", 4: ".spx", 5: ".amr", 7: ".mp3", 8: ".ogg"}


async def _inline_image(path: Path, raw: bytes) -> list[dict[str, str]]:
    mime = _sniff_image_mime(raw)
    if len(raw) <= _INLINE_IMAGE_MAX and path.suffix.lower() not in {".heic", ".bmp", ".tif", ".tiff"}:
        return [{"media_type": mime, "data": base64.b64encode(raw).decode()}]
    small = path.with_name(path.stem + "-inline.jpg")
    try:
        proc = await asyncio.create_subprocess_exec(
            "sips", "-s", "format", "jpeg", "-Z", "2048", str(path), "--out", str(small),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=30)
        data = small.read_bytes()
    except Exception as e:
        log.warning("image shrink failed for %s: %s", path, e)
        return []
    finally:
        small.unlink(missing_ok=True)
    if len(data) > _INLINE_IMAGE_MAX:
        return []
    return [{"media_type": "image/jpeg", "data": base64.b64encode(data).decode()}]


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


async def _decode_item(
    client: WeixinClient, item: dict[str, Any]
) -> tuple[str, list[dict[str, str]]]:
    """One inbound MessageItem → (text_body, images_for_cc).

    Returns text that goes into the CC prompt + base64 image blocks. Voice is
    transcribed; images are inlined; video and files stay on disk with their
    path in the text so the CPU can open, convert or forward them.
    """
    itype = item.get("type")

    if itype == ITEM_TEXT:
        return ((item.get("text_item") or {}).get("text") or "", [])

    if itype == ITEM_VOICE:
        voice = item.get("voice_item") or {}
        media = voice.get("media") or {}
        secs = round((voice.get("playtime") or 0) / 1000)
        label = f"[语音 {secs}s]" if secs else "[语音]"
        if voice.get("text"):  # server-provided transcription (fast path, no download)
            return (f"{label} {voice['text']}", [])
        try:
            raw = await client.download_media(media)
        except Exception as e:
            log.warning("voice download failed: %s", e)
            return (f"[语音下载失败: {e}]", [])
        audio_path = _inbound_tmp(_VOICE_SUFFIXES.get(voice.get("encode_type"), ".silk"))
        audio_path.write_bytes(raw)
        try:
            if audio_path.suffix == ".silk":
                text = await transcribe_silk(audio_path)
            else:
                text = await transcribe_voice(audio_path)
        except Exception as e:
            log.warning("voice transcribe failed: %s", e)
            return (f"[语音转文字失败: {e}; 原始音频: {audio_path}]", [])
        audio_path.unlink(missing_ok=True)
        return (f"{label} {text}", [])

    if itype == ITEM_IMAGE:
        image = item.get("image_item") or {}
        aeskey_hex = image.get("aeskey")
        raw = None
        err: Exception | None = None
        # Original first, then the thumbnail when the original is unavailable.
        for media in (image.get("media"), image.get("thumb_media")):
            if media is None:
                continue
            try:
                raw = await client.download_media(media, aeskey_hex_override=aeskey_hex)
                break
            except Exception as e:
                err = e
        if raw is None:
            log.warning("image download failed: %s", err)
            return (f"[图片下载失败: {err or '消息没有媒体引用'}]", [])
        mime = _sniff_image_mime(raw)
        ext = "jpg" if mime == "image/jpeg" else mime.split("/")[-1]
        img_path = _inbound_tmp(f".{ext}")
        img_path.write_bytes(raw)
        return (f"[图片: {img_path}]", await _inline_image(img_path, raw))

    if itype == ITEM_VIDEO:
        video = item.get("video_item") or {}
        media = video.get("media") or {}
        try:
            raw = await client.download_media(media)
        except Exception as e:
            log.warning("video download failed: %s", e)
            return (f"[视频下载失败: {e}]", [])
        video_path = _inbound_tmp(".mp4")
        video_path.write_bytes(raw)
        secs = round((video.get("play_length") or 0))
        head = f"[视频: {video_path}{f', {secs}s' if secs else ''}, {_human_size(len(raw))}]"
        desc = await understand_video(video_path)
        return (f"{head} {desc}" if desc else head, [])

    if itype == ITEM_FILE:
        f = item.get("file_item") or {}
        media = f.get("media") or {}
        file_name = f.get("file_name") or "file"
        try:
            raw = await client.download_media(media)
        except Exception as e:
            log.warning("file download failed: %s", e)
            return (f"[文件下载失败: {file_name}: {e}]", [])
        safe_name = re.sub(r"[/\\\x00]", "_", file_name)
        _INBOUND_DIR.mkdir(parents=True, exist_ok=True)
        path = _INBOUND_DIR / f"{int(time.time())}-{safe_name}"
        path.write_bytes(raw)
        images = await _inline_image(path, raw) if path.suffix.lower() in _IMAGE_SUFFIXES else []
        return (f"[用户发来文件: {path} ({_human_size(len(raw))})]", images)

    raw_item = json.dumps(item, ensure_ascii=False)[:1500]
    return (f"[未知消息类型 type={itype}: {raw_item}]", [])


_REF_LABELS = {ITEM_IMAGE: "图片", ITEM_VOICE: "语音", ITEM_FILE: "文件", ITEM_VIDEO: "视频"}


def _describe_ref(ref: dict[str, Any] | None, account_id: str = "") -> str:
    if not ref:
        return ""
    title = (ref.get("title") or "").strip()
    item = ref.get("message_item") or {}
    t = item.get("type")
    if t == ITEM_TEXT:
        body = ((item.get("text_item") or {}).get("text") or "").strip()[:500]
        return f"[引用: {body}]" if body else "[引用了一条文本]"
    if t in _REF_LABELS:
        label = _REF_LABELS[t]
        return f"[引用 {label}: {title}]" if title else f"[引用了一条{label}]"
    # Newer clients quote by server id only; resolve from the local cache.
    cached = recall_message(account_id, ref.get("svr_id") or item.get("msg_id"))
    if cached:
        return f"[引用: {cached[:500]}]"
    return f"[引用: {title}]" if title else "[引用了一条消息，原文未缓存]"


# ── turn gathering ───────────────────────────────────────────────────
# WeChat has no album grouping and media lands only after its CDN upload, so a
# caption or "看这个文件" can arrive before the image/file it refers to. Polling
# is paused while a turn runs, so the poll loop keeps listening briefly after a
# batch and merges stragglers into the same turn.
_WX_GRACE_S = float(os.environ.get("BABATA_WX_GRACE_S", "2.5"))
_WX_MEDIA_GRACE_S = 8.0   # text mentions media that has not arrived yet
_WX_GRACE_MAX_S = 15.0
_MEDIA_HINT_RE = re.compile(r"图|照片|截图|文件|附件|视频|语音|录音|pdf|PDF|发了|发你|看下这|看看这")
# A recoverable CPU failure must not permanently drop the inbound WeChat turn.
# Retry the same decoded burst before the caller advances to later updates.
_WX_MAX_TURN_RECOVERY_ATTEMPTS = int(
    os.environ.get(
        "BABATA_WX_TURN_RECOVERY_ATTEMPTS",
        os.environ.get("BABATA_TURN_RECOVERY_ATTEMPTS", "2"),
    )
)
_pending_wx_lock = asyncio.Lock()


def _awaits_media(msgs: list[dict[str, Any]]) -> bool:
    items = [it for m in msgs if m.get("message_type") == 1 for it in (m.get("item_list") or [])]
    if any(it.get("type") != ITEM_TEXT for it in items):
        return False
    return any(
        _MEDIA_HINT_RE.search((it.get("text_item") or {}).get("text") or "") for it in items
    )


async def _gather_stragglers(
    client: WeixinClient, buf: str, msgs: list[dict[str, Any]],
) -> str:
    """Short follow-up polls appending late messages to `msgs`. Returns the newest buf."""
    if _WX_GRACE_S <= 0:
        return buf
    deadline = time.monotonic() + _WX_GRACE_MAX_S
    while (remaining := deadline - time.monotonic()) > 0:
        wait = _WX_MEDIA_GRACE_S if _awaits_media(msgs) else _WX_GRACE_S
        try:
            more = await asyncio.wait_for(client.get_updates(buf), timeout=min(wait, remaining))
        except asyncio.TimeoutError:
            break  # the cursor is client-held, so an abandoned poll loses nothing
        except Exception as e:
            log.debug("straggler poll stopped: %s", e)
            break
        late = more.get("msgs") or []
        buf = more.get("get_updates_buf", buf)
        if not late:
            break
        msgs.extend(late)
    return buf


def _load_pending_wx_updates() -> dict[str, dict[str, Any]]:
    if not PENDING_WX_UPDATES_FILE.exists():
        return {}
    try:
        data = json.loads(PENDING_WX_UPDATES_FILE.read_text())
        records = data.get("pending", {})
        if isinstance(records, dict):
            return {str(k): v for k, v in records.items() if isinstance(v, dict)}
    except Exception as e:
        log.warning("wx pending-updates load failed: %s, treating as empty", e)
    return {}


_pending_wx_records: dict[str, dict[str, Any]] = _load_pending_wx_updates()


def _write_pending_wx_locked() -> None:
    tmp = PENDING_WX_UPDATES_FILE.with_suffix(".json.partial")
    PENDING_WX_UPDATES_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(
        json.dumps(
            {"pending": _pending_wx_records},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    os.replace(tmp, PENDING_WX_UPDATES_FILE)


async def _record_pending_wx_batch(
    account_id: str,
    *,
    new_buf: str,
    units: list[list[dict[str, Any]]],
) -> str:
    record_id = f"{int(time.time() * 1000)}-{secrets.token_hex(4)}"
    record = {
        "id": record_id,
        "account_id": account_id,
        "new_buf": new_buf,
        "units": units,
        "received_at": time.time(),
    }
    async with _pending_wx_lock:
        _pending_wx_records[record_id] = record
        _write_pending_wx_locked()
    return record_id


async def _ack_pending_wx_batch(record_id: str) -> None:
    async with _pending_wx_lock:
        if record_id not in _pending_wx_records:
            return
        _pending_wx_records.pop(record_id, None)
        _write_pending_wx_locked()


async def _pending_wx_batches_for_account(account_id: str) -> list[dict[str, Any]]:
    async with _pending_wx_lock:
        records = [
            dict(record)
            for record in _pending_wx_records.values()
            if record.get("account_id") == account_id
        ]
    return sorted(records, key=lambda r: r.get("received_at", 0))


# Single-flight across all users/accounts: concurrent CC queries would race
# on the shared session resume (session_id written per query end).
_cc_lock = asyncio.Lock()


async def _enqueue_inbound_msg(
    client: WeixinClient, msg: dict[str, Any], account_id: str
) -> bool:
    return await _enqueue_inbound_msgs(client, [msg], account_id)


async def _enqueue_inbound_msgs(
    client: WeixinClient, msgs: list[dict[str, Any]], account_id: str
) -> bool:
    if not msgs:
        return True

    from_user = msgs[0].get("from_user_id") or ""
    for msg in msgs:
        ctx_token = msg.get("context_token")
        if from_user and ctx_token:
            set_context_token(account_id, from_user, ctx_token)

    if not is_allowed(account_id, from_user):
        log.warning("ignoring unauthorized from=%s", from_user)
        return True

    async with _cc_lock:
        # Sends that hit the reply quota last time go out first, on this message's token.
        newest = next((m.get("context_token") for m in reversed(msgs) if m.get("context_token")), None)
        try:
            await client.flush_backlog(from_user, newest)
        except Exception:
            log.exception("reply backlog flush failed")
        try:
            return await _process_combined_msgs(client, msgs, account_id)
        except Exception:
            log.exception("msg processing crashed")
            return False


def _format_wx_message_bodies(message_bodies: list[str]) -> str:
    combined = "\n".join(message_bodies).strip()
    if len(message_bodies) <= 1:
        return _truncate_wx_input(combined)
    blocks = [
        "Multiple WeChat messages, oldest to newest.",
        "",
    ]
    for idx, body in enumerate(message_bodies, start=1):
        blocks.append(f"<user_message n={idx}>\n{body}\n</user_message>")
        blocks.append("")
    return _truncate_wx_input("\n".join(blocks).strip())


def _truncate_wx_input(text: str) -> str:
    limit = max(1, _WX_INPUT_MAX_CHARS)
    if len(text) <= limit:
        return text
    keep = max(0, limit - len(_WX_INPUT_TRUNCATED_NOTE))
    if keep <= 0:
        return _WX_INPUT_TRUNCATED_NOTE[-limit:]
    return text[:keep].rstrip() + _WX_INPUT_TRUNCATED_NOTE


async def _collect_wx_turn_inputs(
    client: WeixinClient,
    msgs: list[dict[str, Any]],
    account_id: str = "",
) -> tuple[str, str | None, str, list[dict[str, str]]]:
    from_user = msgs[0].get("from_user_id") or ""
    ctx_token = next(
        (m.get("context_token") for m in reversed(msgs) if m.get("context_token")),
        None,
    )
    images: list[dict[str, str]] = []
    message_bodies: list[str] = []
    for msg in msgs:
        texts: list[str] = []
        ref_note = ""
        for item in msg.get("item_list") or []:
            ref = item.get("ref_msg")
            if ref:
                ref_note = _describe_ref(ref, account_id)
                ref_item = ref.get("message_item") or {}
                if ref_item.get("type") in _REF_LABELS:
                    # Quoted media is downloaded so the CPU sees what was quoted.
                    ref_text, ref_imgs = await _decode_item(client, ref_item)
                    ref_note = f"{ref_note} {ref_text}".strip()
                    images.extend(ref_imgs)
            text, imgs = await _decode_item(client, item)
            if text:
                texts.append(text)
            images.extend(imgs)
        body = "\n".join(t for t in texts if t).strip()
        remember_message(account_id, msg.get("message_id"), body)
        if ref_note:
            body = f"{ref_note}\n{body}" if body else ref_note
        if body:
            message_bodies.append(body)
    return from_user, ctx_token, _format_wx_message_bodies(message_bodies), images


async def _set_wx_typing(
    client: WeixinClient,
    from_user: str,
    ticket: str | None,
    state: int,
) -> None:
    if not ticket:
        return
    try:
        await client.send_typing(from_user, ticket, state)
    except Exception as e:
        if state == 1:
            log.debug("typing on failed: %s", e)


async def _query_wx_turn_with_recovery(
    prompt: str,
    images: list[dict[str, str]],
    coalescer: WxStreamCoalescer,
) -> Any | None:
    for attempt in range(_WX_MAX_TURN_RECOVERY_ATTEMPTS + 1):
        if attempt:
            coalescer.reset_for_replay()
            log.warning(
                "Replaying WeChat turn after CC query failure "
                "(attempt %d/%d)",
                attempt,
                _WX_MAX_TURN_RECOVERY_ATTEMPTS,
            )
        try:
            resp = await cc.query(
                prompt,
                images=images or None,
                on_stream=coalescer.on_stream,
            )
            if getattr(resp, "cpu_unavailable", False):
                if fallback_cc:
                    log.warning("primary CPU unavailable (quota/auth); WeChat turn → fallback")
                    resp = await fallback_cc.query(
                        prompt,
                        images=images or None,
                        on_stream=coalescer.on_stream,
                    )
                else:
                    log.warning("primary CPU unavailable (quota/auth); no fallback configured")
                    resp.content = (
                        "⚠️ Claude 暂时不可用（额度或登录），这条消息没有处理，恢复后请重发。\n\n"
                        f"{resp.content or ''}"
                    ).strip()
            return resp
        except Exception as e:
            log.exception("CC query failed")
            if attempt < _WX_MAX_TURN_RECOVERY_ATTEMPTS:
                await asyncio.sleep(0)
                continue
            log.error("WeChat turn remains unconsumed after retries: %s", e)
            return None
    return None


async def _replay_wx_final_if_needed(coalescer: WxStreamCoalescer, final: str) -> bool:
    """Ensure final content is visible when streaming did not deliver it safely.

    WeChat has no edit-message. If streaming already sent clean bubbles, keep
    them. If streaming sent nothing or lost chunks mid-stream, replay
    resp.content as the fallback visible answer.
    """
    if not coalescer.had_send_failure and coalescer.sent_any:
        return True
    if final:
        coalescer.had_send_failure = False
        for part in re.split(r"\n{3,}", final):
            await coalescer.send_bubble(part)
        return True
    return not coalescer.had_send_failure


async def _process_combined_msgs(
    client: WeixinClient, msgs: list[dict[str, Any]], account_id: str
) -> bool:
    if not msgs:
        return True

    from_user, ctx_token, combined, images = await _collect_wx_turn_inputs(
        client, msgs, account_id,
    )
    if not combined and not images:
        log.info("inbound from %s: no decodable content", from_user)
        return True

    log.info("← %s: %s (imgs=%d, msgs=%d)", from_user, combined[:80], len(images), len(msgs))

    # Hand bridge the current conversation context so wx_mcp actions can reply
    bridge.set_context(client, from_user, ctx_token, account_id)

    # Typing on (best-effort; ticket cached across inbounds, 24h TTL)
    ticket = await _get_typing_ticket(client, from_user, ctx_token)
    await _set_wx_typing(client, from_user, ticket, 1)

    # Stream coalescer — wx protocol has no edit-message, so each bubble is
    # final once sent. LLM marks bubble boundaries with \n\n\n in its output
    # (see _WX_SOURCE_PROMPT); _drain splits buf on that marker, ships the
    # complete bubbles, and holds the trailing partial. Idle / hard-max are
    # safety nets for when the LLM hangs mid-bubble or forgets to mark; they
    # split at safe boundaries (paragraph / line / sentence-end) rather than
    # mid-word, and only hard-cut at end-of-stream when nothing safe exists.
    async def send_bubble(text: str) -> WxBubbleSendResult:
        return await _send_wx_bubble(client, from_user, ctx_token, text, account_id)

    coalescer = WxStreamCoalescer(send_bubble, lambda: client.quota_left(ctx_token))

    resp = await _query_wx_turn_with_recovery(combined or "[图片]", images, coalescer)
    if resp is None:
        return False

    # End-of-stream drain: ship any held tail now that nothing more is coming.
    await coalescer.drain(allow_hard_cut=True)

    # Native Codex image generation returns local files separately from text.
    # WeChat must upload them; a local Markdown path is not a visible image.
    for raw_path in resp.generated_images:
        path = Path(raw_path).expanduser()
        try:
            await _send_wx_image(client, from_user, ctx_token, path)
        except WeixinQuotaExhausted:
            continue
        except Exception as e:
            log.warning("generated image delivery failed: %s: %s", path, e)
            await _set_wx_typing(client, from_user, ticket, 2)
            return False

    if not await _replay_wx_final_if_needed(coalescer, resp.content or ""):
        return False

    if resp.resume_note:
        try:
            await client.send_message(
                from_user, [text_item(resp.resume_note)], context_token=ctx_token,
            )
        except Exception:
            pass

    await _set_wx_typing(client, from_user, ticket, 2)

    return not coalescer.had_send_failure


def _coalesce_update_msgs(msgs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    units: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_from = None
    for msg in msgs:
        if msg.get("message_type") != 1:  # USER only (ignore BOT echoes)
            continue
        from_user = msg.get("from_user_id") or ""
        if current and from_user != current_from:
            units.append(current)
            current = []
        current.append(msg)
        current_from = from_user
    if current:
        units.append(current)
    return units


async def _process_wx_units(
    client: WeixinClient,
    account_id: str,
    units: list[list[dict[str, Any]]],
) -> bool:
    batch_ok = True
    for msg_unit in units:
        try:
            if len(msg_unit) == 1:
                result = await _enqueue_inbound_msg(client, msg_unit[0], account_id)
            else:
                result = await _enqueue_inbound_msgs(client, msg_unit, account_id)
            if result is False:
                batch_ok = False
        except Exception:
            batch_ok = False
            log.exception("msg enqueue crashed")
    return batch_ok


def _wx_message_key(msg: Any) -> tuple[str, str] | None:
    if not isinstance(msg, dict):
        return None
    message_id = msg.get("message_id")
    from_user = msg.get("from_user_id")
    if message_id is None or message_id == "" or not from_user:
        return None
    return str(from_user), str(message_id)


async def _replay_pending_wx_updates(
    client: WeixinClient,
    account_id: str,
) -> None:
    consumed: set[tuple[str, str]] = set()
    for record in await _pending_wx_batches_for_account(account_id):
        record_id = str(record.get("id") or "")
        new_buf = str(record.get("new_buf") or "")
        units = record.get("units")
        if not record_id or not isinstance(units, list):
            log.warning("dropping malformed wx pending record: %s", record_id or "?")
            if record_id:
                await _ack_pending_wx_batch(record_id)
            continue
        if new_buf and load_sync_buf(account_id) == new_buf:
            consumed.update(
                key for unit in units if isinstance(unit, list) for msg in unit
                if (key := _wx_message_key(msg)) is not None
            )
            await _ack_pending_wx_batch(record_id)
            continue
        # Failed polls can journal the same message under different cursors.
        # Replay each known message once while retaining later new messages.
        fresh_units = []
        batch_keys: set[tuple[str, str]] = set()
        for unit in units:
            if not isinstance(unit, list):
                fresh_units.append(unit)
                continue
            fresh = []
            for msg in unit:
                key = _wx_message_key(msg)
                if key is not None:
                    if key in consumed or key in batch_keys:
                        continue
                    batch_keys.add(key)
                fresh.append(msg)
            if fresh:
                fresh_units.append(fresh)
        ok = not fresh_units or await _process_wx_units(client, account_id, fresh_units)
        if not ok:
            log.warning("wx pending replay still unconsumed: %s", record_id)
            break
        consumed.update(batch_keys)
        if new_buf:
            save_sync_buf(account_id, new_buf)
        await _ack_pending_wx_batch(record_id)
        log.warning("replayed unconsumed WX update batch: %s", record_id)


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
        quota_path=reply_quota_path(account_id),
    )
    # Make the live client available to bridge before any inbound message.
    # Enables proactive cron pushes (e.g. codex2api-grabber) on bot boot —
    # to/context_token still fall back to persistent peer (see
    # weixin_bridge._restore_peer_from_disk).
    bridge.client = client
    bridge.account_id = account_id
    log.info("long-poll starting for %s", account_id)

    asyncio.create_task(_heartbeat_loop(client, account_id))

    await _replay_pending_wx_updates(client, account_id)
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
        msgs = list(resp.get("msgs") or [])
        if _coalesce_update_msgs(msgs):
            new_buf = await _gather_stragglers(client, new_buf, msgs)
        units = _coalesce_update_msgs(msgs)
        pending_record_id = ""
        batch_ok = True
        if units:
            try:
                pending_record_id = await _record_pending_wx_batch(
                    account_id,
                    new_buf=new_buf,
                    units=units,
                )
            except Exception:
                batch_ok = False
                log.exception("wx pending-updates write failed")
            if batch_ok:
                batch_ok = await _process_wx_units(client, account_id, units)
        if batch_ok and new_buf != buf:
            save_sync_buf(account_id, new_buf)
            buf = new_buf
        if batch_ok and pending_record_id:
            await _ack_pending_wx_batch(pending_record_id)


# Distinguishes two exit-0 paths so launchd KeepAlive { SuccessfulExit: false }
# only suppresses relaunch for the lock-loser path. SIGTERM/SIGINT from system
# (sleep wake reconcile, OOM, ad-hoc kill) flips this to True → __main__ forces
# exit 1 so launchd respawns. Without it, any graceful signal stays exit 0 and
# the bot stays dead until manual kickstart (2026-05-06 incident, codex review).
_signal_received = False


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
        global _signal_received
        log.info("stop signal received…")
        _signal_received = True
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


_SINGLETON_LOCK_PATH = Path("/tmp/babata-weixin.lock")
_singleton_lock_fd = None  # module-global so fd stays open for process lifetime


def _acquire_singleton_lock() -> None:
    """OS-level singleton: one weixin_bot per host.

    Multiple launchd plists / OSS spawn paths / manual double-clicks all
    converge here. Lock holder owns /tmp/babata-weixin-bridge.sock and the
    iLink long-poll cursor. Lock auto-releases on process death (BSD flock).

    Without this guard, weixin_bridge.start() does os.unlink(SOCKET_PATH)
    before bind, so a second instance silently steals the socket from a
    live first instance — both keep long-polling iLink, race-handling each
    inbound, WeChat replies get duplicated.

    Loser behavior:
      - normal mode: exit 0. Pair with KeepAlive dict + SuccessfulExit=false
        in any launchd plist so launchd does NOT relaunch on intentional
        bow-out (default KeepAlive=true would spin-loop every ThrottleInterval).
      - --login mode: exit 1 with explicit error (interactive account-add;
        silent exit would confuse the user).

    Open with "a+" not "w" so a loser does not truncate the holder's PID
    breadcrumb before reading it for the log.
    """
    global _singleton_lock_fd
    _singleton_lock_fd = open(_SINGLETON_LOCK_PATH, "a+")
    try:
        fcntl.flock(_singleton_lock_fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _singleton_lock_fd.seek(0)
        try:
            other = _singleton_lock_fd.read().strip() or "?"
        except Exception:
            other = "?"
        if "--login" in sys.argv:
            log.error(
                "another weixin_bot holds %s (pid=%s) — "
                "stop the running bot before adding an account",
                _SINGLETON_LOCK_PATH, other,
            )
            sys.exit(1)
        log.warning(
            "another weixin_bot holds %s (pid=%s) — exiting",
            _SINGLETON_LOCK_PATH, other,
        )
        sys.exit(0)
    _singleton_lock_fd.seek(0)
    _singleton_lock_fd.truncate()
    _singleton_lock_fd.write(f"{os.getpid()}\n")
    _singleton_lock_fd.flush()


if __name__ == "__main__":
    _acquire_singleton_lock()
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nstopped.")
    if _signal_received:
        sys.exit(1)
