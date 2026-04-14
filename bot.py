"""CC TG Bot — thin Telegram transport for Claude Code.

TG is just a channel. The only difference from terminal CC is the wire.
Bot only does what CC physically cannot: TG transport, media conversion, UI feedback.
"""

import html
import json
import logging
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(override=True)  # Must run before importing media (which reads env at import time)

from telegram import Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bridge import bridge
from cc import CC
from media import image_to_base64, transcribe_voice, understand_video

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER = int(os.environ.get("ALLOWED_USER_ID", "0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger("cc-tg")

cc = CC()

# User preferences persisted across restarts
_STATE_PATH = Path.home() / ".cc-tg-state.json"


def _load_state() -> dict:
    try:
        return json.loads(_STATE_PATH.read_text())
    except Exception:
        return {}


def _save_state() -> None:
    try:
        _STATE_PATH.write_text(json.dumps(_state))
    except Exception:
        pass


_state = _load_state()
# Tool display mode: 0=hidden, 1=show then delete, 2=show and keep
_verbose = _state.get("verbose", 1)

# ── Formatting (physical: TG requires HTML, max 4096 chars) ──────────

_MAX_TG = 4000

_TOOL_ICONS = {
    "Read": "\U0001f4d6", "Write": "\u270f\ufe0f", "Edit": "\u270f\ufe0f",
    "Bash": "\U0001f4bb", "Glob": "\U0001f50d", "Grep": "\U0001f50d",
    "WebFetch": "\U0001f310", "WebSearch": "\U0001f310",
    "Agent": "\U0001f916", "Task": "\U0001f9e0",
}


def _icon(name: str) -> str:
    return _TOOL_ICONS.get(name, "\U0001f527")


def _to_html(md: str) -> str:
    """Best-effort markdown → TG HTML."""
    if not md:
        return ""
    blocks: list[str] = []

    def _save_block(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = html.escape(m.group(2))
        blocks.append(
            f'<pre><code class="language-{lang}">{code}</code></pre>' if lang
            else f"<pre>{code}</pre>"
        )
        return f"\x00BLK{len(blocks) - 1}\x00"

    text = re.sub(r"```(\w*)\n(.*?)```", _save_block, md, flags=re.DOTALL)
    text = html.escape(text)
    text = re.sub(r"`([^`]+)`", r"<code>\1</code>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    for i, blk in enumerate(blocks):
        text = text.replace(f"\x00BLK{i}\x00", blk)
    return text.strip()


def _split(text: str) -> list[str]:
    """Split long message at newlines with chunk indicators."""
    if len(text) <= _MAX_TG:
        return [text]
    parts: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= _MAX_TG:
            parts.append(remaining)
            break
        idx = remaining.rfind("\n", 0, _MAX_TG)
        if idx == -1:
            idx = _MAX_TG
        parts.append(remaining[:idx])
        remaining = remaining[idx:].lstrip("\n")
    if len(parts) > 1:
        parts = [f"{p}\n\n({i+1}/{len(parts)})" for i, p in enumerate(parts)]
    return parts


# ── Reactions (physical: TG-specific UI feedback) ─────────────────────

async def _react(msg, emoji: str) -> None:
    try:
        from telegram import ReactionTypeEmoji
        await msg.set_reaction([ReactionTypeEmoji(emoji=emoji)])
    except Exception:
        pass


# ── Auth (physical: access control) ──────────────────────────────────

def _allowed(update: Update) -> bool:
    if not ALLOWED_USER:
        return True
    return bool(update.effective_user and update.effective_user.id == ALLOWED_USER)


# ── Handlers ──────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    await update.message.reply_text("Ready.")


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    cc.reset()
    await update.message.reply_text("Session reset.")


async def cmd_verbose(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    labels = {0: "hidden", 1: "flash", 2: "keep"}
    buttons = [
        [InlineKeyboardButton(
            f"{'> ' if _verbose == i else ''}{v}",
            callback_data=f"verbose:{i}",
        ) for i, v in labels.items()]
    ]
    await update.message.reply_text(
        "Tool display:", reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    await _process(update, ctx, update.message.text)


async def on_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    file = await ctx.bot.get_file(voice.file_id)
    path = Path(f"/tmp/voice_{voice.file_id}.ogg")
    await file.download_to_drive(path)

    text = await transcribe_voice(path)
    path.unlink(missing_ok=True)

    if not text:
        await update.message.reply_text("Could not transcribe voice.")
        return

    await update.message.reply_text(f"\U0001f3a4 {text}")
    await _process(update, ctx, text)


async def on_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    photos = update.message.photo
    if not photos:
        return

    photo = photos[-1]
    file = await ctx.bot.get_file(photo.file_id)
    path = Path(f"/tmp/photo_{photo.file_id}.jpg")
    await file.download_to_drive(path)

    images = [image_to_base64(path)]
    path.unlink(missing_ok=True)

    caption = update.message.caption or "What's in this image?"
    await _process(update, ctx, caption, images=images)


async def on_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    video = update.message.video or update.message.video_note
    if not video:
        return

    file = await ctx.bot.get_file(video.file_id)
    path = Path(f"/tmp/video_{video.file_id}.mp4")
    await file.download_to_drive(path)

    caption = update.message.caption or ""
    summary = await understand_video(path, caption)
    path.unlink(missing_ok=True)

    if not summary:
        await update.message.reply_text(
            "Video understanding unavailable. Set VIDEO_API_URL or keep video <10MB."
        )
        return

    user_text = caption or "我发了一段视频给你。"
    await _process(
        update, ctx,
        f"{user_text}\n\n[Video summary (mimo-v2-omni)]: {summary}",
    )


async def on_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    doc = update.message.document
    if not doc:
        return

    file = await ctx.bot.get_file(doc.file_id)
    save_dir = Path.home() / "Downloads"
    save_dir.mkdir(exist_ok=True)
    save_path = save_dir / doc.file_name
    await file.download_to_drive(save_path)

    caption = update.message.caption or f"File saved to {save_path}"
    await _process(update, ctx, caption)


async def on_verbose_click(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    global _verbose
    _verbose = int((query.data or "verbose:1").split(":")[1])
    _state["verbose"] = _verbose
    _save_state()
    labels = {0: "hidden", 1: "flash", 2: "keep"}
    await query.edit_message_text(f"Tool display: {labels[_verbose]}")


async def on_button_click(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle MCP button click: resolve bridge future, send choice to CC."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("mcp:"):
        return

    parts = data.split(":", 2)
    if len(parts) < 2:
        return

    option_index = int(parts[1])
    msg_id = query.message.message_id
    label = parts[2] if len(parts) > 2 else str(option_index)

    # Always remove buttons and show selection
    try:
        original = query.message.text or ""
        await query.edit_message_text(f"{original}\n\n\u2705 {label}")
    except Exception:
        pass

    # Try to resolve MCP future (CC is waiting for this)
    if not bridge.resolve(msg_id, option_index, label):
        # MCP future gone (timeout/session ended) — send choice as new message to CC
        await _process(update, ctx, label)


# ── Core flow ─────────────────────────────────────────────────────────

async def _process(
    update: Update,
    ctx: ContextTypes.DEFAULT_TYPE,
    text: str,
    images: list[dict[str, str]] | None = None,
) -> None:
    """Send to CC, stream tool activity, deliver response."""
    chat = update.effective_chat
    msg = update.effective_message
    await chat.send_action("typing")

    # Physical: reply/quote content isn't in msg.text, must prepend
    reply = getattr(msg, "reply_to_message", None)
    if reply:
        quote = getattr(msg, "quote", None)
        quoted = (
            (quote and quote.text)
            or reply.text or reply.caption
            or (reply.document and f"[a file: {reply.document.file_name}]")
            or (reply.photo and "[a photo]")
            or (reply.voice and "[a voice]")
            or (reply.audio and "[an audio]")
            or "[a message]"
        )
        text = f"[Replying to]: {quoted}\n\n{text}"

    # Set bridge context so MCP tools can send to this chat
    bridge.set_context(ctx.bot, chat.id, msg.message_id)

    # React: eyes = processing
    await _react(msg, "\U0001f440")

    # Status message for streaming tool names
    status = await msg.reply_text("\u23f3") if _verbose > 0 else None
    tools: list[str] = []
    last_edit = 0.0

    async def _on_stream(tool_name: str | None, text_chunk: str | None) -> None:
        nonlocal last_edit
        if not tool_name:
            return
        if tool_name not in tools:
            tools.append(tool_name)

        if not status or _verbose == 0:
            return

        now = time.monotonic()
        if now - last_edit < 2.0:
            return
        last_edit = now

        line = " \u2192 ".join(f"{_icon(t)} {t}" for t in tools)
        try:
            await status.edit_text(line)
        except Exception:
            pass
        try:
            await chat.send_action("typing")
        except Exception:
            pass

    try:
        resp = await cc.query(text, images=images, on_stream=_on_stream)
    except Exception as e:
        log.error("CC query failed: %s", e)
        await _react(msg, "\U0001f44e")
        if status:
            await status.edit_text(f"Error: {e}")
        return

    # Clean up status: delete if mode 1, keep if mode 2, absent if mode 0
    if status and _verbose == 1:
        try:
            await status.delete()
        except Exception:
            pass

    # React: thumbs up = done
    await _react(msg, "\U0001f44d")

    if not resp.content:
        await msg.reply_text("(no response)")
        return

    html_text = _to_html(resp.content)
    parts = _split(html_text)

    for part in parts:
        try:
            await msg.reply_text(part, parse_mode="HTML")
        except Exception:
            plain_parts = _split(resp.content)
            for pp in plain_parts:
                await msg.reply_text(pp[:_MAX_TG])
            break

    if resp.cost > 0:
        log.info("Cost: $%.4f | Session: %s", resp.cost, resp.session_id[:8] if resp.session_id else "new")


# ── Main ──────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    await bridge.start()
    # Default context so terminal CC (no TG message yet) can push to user's TG
    if ALLOWED_USER:
        bridge.set_context(app.bot, ALLOWED_USER, None)
    await app.bot.set_my_commands([
        ("new", "Start a fresh session"),
        ("verbose", "Tool display: 0=hidden 1=flash 2=keep"),
    ])


def main() -> None:
    app = Application.builder().token(TOKEN).concurrent_updates(True).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("verbose", cmd_verbose))
    app.add_handler(CallbackQueryHandler(on_verbose_click, pattern=r"^verbose:"))
    app.add_handler(CallbackQueryHandler(on_button_click, pattern=r"^mcp:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, on_video))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    log.info("Bot starting (user: %s)", ALLOWED_USER)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
