"""CC TG Bot — thin Telegram transport for Claude Code.

TG is just a channel. The only difference from terminal CC is the wire.
Bot only does what CC physically cannot: TG transport, media conversion, UI feedback.
"""

import asyncio
import html
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import time
from pathlib import Path

from dotenv import load_dotenv

# override=False: plist-injected env (per-instance TELEGRAM_BOT_TOKEN /
# BABATA_INSTANCE / ALLOWED_USER_ID) wins over .env defaults. Without this,
# two bot instances launched from the same repo would all grab the same .env
# token. Must run before importing media (which reads env at import time)
# and before `from constants` (which reads BABATA_INSTANCE / PROJECT_NAMESPACE).
load_dotenv(override=False)

# Namespace / paths derive from PROJECT_NAMESPACE + BABATA_INSTANCE env.
# See constants.py for the full derivation. Propagate BRIDGE_SOCKET to
# bridge.py (imported next) and to tg_mcp subprocess below.
from constants import BRIDGE_SOCKET, INSTANCE, INSTANCE_LABELS, PROJECT, SESSION_FILE, STATE_FILE
os.environ["BABATA_BRIDGE_SOCKET"] = BRIDGE_SOCKET

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
from cc import CC, VENV_PYTHON
from media import image_to_base64, transcribe_voice, understand_video

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER = int(os.environ.get("ALLOWED_USER_ID", "0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger(PROJECT)

_TG_MCP_SCRIPT = str(Path(__file__).parent / "tg_mcp.py")

_TG_SOURCE_PROMPT = "Source: Telegram."

cc = CC(
    state_file=SESSION_FILE,
    source_prompt=_TG_SOURCE_PROMPT,
    mcp_servers={
        "tg": {
            "command": VENV_PYTHON,
            "args": [_TG_MCP_SCRIPT],
            # Route the MCP subprocess to this instance's bridge socket. CC CLI
            # merges this with inherited env when spawning stdio MCP servers.
            "env": {"BABATA_BRIDGE_SOCKET": BRIDGE_SOCKET},
        },
    },
)

# ── Graceful shutdown ─────────────────────────────────────────────────
# SIGTERM / SIGINT / /restart 都走 _graceful_shutdown: 若有 CC 任务在跑
# (_in_flight > 0), 先推 TG 告知, 等跑完再退. launchd plist 的 ExitTimeOut
# 必须调高 (默认 20s, babata 设 600s) 否则 SIGKILL 会强杀.
#
# 只保护 cc.query 的 await 期间 —— 后续 TG 消息 finalize 是毫秒级, 即便被
# 打断 CC session jsonl 已 flush, V 从 /resume 仍能完整读到.
_in_flight = 0                  # concurrent CC queries (PTB concurrent_updates)
_shutdown_requested = False     # debounce: 第二次信号 → 强退


def _inflight_enter() -> None:
    global _in_flight
    _in_flight += 1


def _inflight_exit() -> None:
    global _in_flight
    _in_flight = max(0, _in_flight - 1)


async def _wait_inflight_drain(poll: float = 0.5) -> None:
    while _in_flight > 0:
        await asyncio.sleep(poll)


async def _graceful_shutdown(app: "Application", reason: str) -> None:
    """Wait for in-flight CC queries, notify V via TG, then exit."""
    global _shutdown_requested
    if _shutdown_requested:
        log.warning("Second shutdown signal (%s), force exit", reason)
        os._exit(1)
    _shutdown_requested = True
    log.info("Graceful shutdown requested: %s (in_flight=%d)", reason, _in_flight)

    if _in_flight > 0 and ALLOWED_USER:
        try:
            await app.bot.send_message(
                ALLOWED_USER,
                f"[{_CURRENT_LABEL}] {reason} · 等 {_in_flight} 个任务跑完再重启",
            )
        except Exception as e:
            log.warning("Pre-shutdown notice failed: %s", e)

    await _wait_inflight_drain()

    if ALLOWED_USER:
        try:
            await app.bot.send_message(
                ALLOWED_USER,
                f"[{_CURRENT_LABEL}] 重启中... (launchd 自愈, ~10s 回来)",
            )
        except Exception as e:
            log.warning("Shutdown notice failed: %s", e)

    # 让 TG round-trip 送出消息再死
    await asyncio.sleep(0.5)
    log.warning("Graceful shutdown complete, exiting pid=%d", os.getpid())
    os._exit(0)


def _install_signal_handlers(app: "Application") -> None:
    """覆盖 PTB 默认 SIGTERM/SIGINT handler, 走 graceful 路径.

    必须在 run_polling(stop_signals=None) 下调用, 否则 PTB 会注册自己的 handler
    立即停止, graceful 逻辑没机会执行.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(
            sig,
            lambda s=sig: asyncio.create_task(
                _graceful_shutdown(app, reason=f"收到 {s.name}")
            ),
        )

# ── Heartbeat (双 bot 互监控, 零 LLM 成本) ──────────────────────────
# 自己每 30s touch; 主 TG bot (INSTANCE="") 监控微信心跳, stale > 3 min 告警
# 一次 (对方 fresh 后允许再告). vvv/vvvv 只写不监控 — 避免重复告警.

_HEARTBEAT_DIR = Path.home() / "cc-workspace" / "state"
_HEARTBEAT_ME = _HEARTBEAT_DIR / f"babata-tg{'-' + INSTANCE if INSTANCE else ''}-heartbeat"
_HEARTBEAT_PEER = _HEARTBEAT_DIR / "babata-weixin-heartbeat"
_HEARTBEAT_STALE_S = 180
_HEARTBEAT_INTERVAL_S = 30


async def _heartbeat_loop(app: "Application") -> None:
    _HEARTBEAT_DIR.mkdir(parents=True, exist_ok=True)
    alerted = False
    is_primary = not INSTANCE
    while True:
        try:
            _HEARTBEAT_ME.touch()
            if is_primary and _HEARTBEAT_PEER.exists():
                age = time.time() - _HEARTBEAT_PEER.stat().st_mtime
                if age > _HEARTBEAT_STALE_S and not alerted and ALLOWED_USER:
                    try:
                        await app.bot.send_message(
                            ALLOWED_USER,
                            f"⚠️ 微信 bot 心跳已 {int(age)}s 未更新 (阈值 {_HEARTBEAT_STALE_S}s)",
                        )
                        alerted = True
                    except Exception as e:
                        log.warning("heartbeat alert send failed: %s", e)
                elif age <= 60:
                    alerted = False
        except Exception as e:
            log.warning("heartbeat loop error: %s", e)
        await asyncio.sleep(_HEARTBEAT_INTERVAL_S)


# User preferences persisted across restarts.
_STATE_PATH = STATE_FILE


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
_verbose = int(_state.get("verbose", 1))

# /status accounting. Per-session accumulators — reset on /new via cc.reset().
# Persisted to state.json so /status survives bot restarts (otherwise the
# first /status after kickstart shows "(no turn yet)").
_session_cost = float(_state.get("session_cost", 0.0))
_session_turns = int(_state.get("session_turns", 0))
_last_model: str | None = _state.get("last_model")
_last_context_window: int | None = _state.get("last_context_window")
_last_used_tokens: int = int(_state.get("last_used_tokens", 0))
_last_cost: float = float(_state.get("last_cost", 0.0))

# ── Formatting (physical: TG requires HTML, max 4096 chars) ──────────

_MAX_TG = 4000

_TOOL_EMOJI = {
    "Bash": "\U0001f4bb",           # 💻 terminal
    "Read": "\U0001f4d6",           # 📖 book
    "Write": "\u270d\ufe0f",        # ✍️ writing hand
    "Edit": "\U0001f527",           # 🔧 patch
    "MultiEdit": "\U0001f527",      # 🔧
    "Glob": "\U0001f4c2",           # 📂 file match
    "Grep": "\U0001f50d",           # 🔍 content search
    "WebFetch": "\U0001f4c4",       # 📄 page fetch
    "WebSearch": "\U0001f310",      # 🌐 web search
    "Task": "\U0001f500",           # 🔀 delegate
    "TodoWrite": "\u2705",          # ✅
    "NotebookEdit": "\U0001f4d3",   # 📓
    "Skill": "\U0001f4da",          # 📚 skill library
    "ToolSearch": "\U0001f9f0",     # 🧰 toolbox
}

def _fmt_tool(name: str, inp: dict) -> str:
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        display = f"{parts[1]}/{parts[2]}" if len(parts) >= 3 else name
        emoji = "\U0001f9e9"  # 🧩 MCP plugin
    else:
        display = name
        emoji = _TOOL_EMOJI.get(name, "\U0001f527")
    # First non-empty string value — dict is insertion-ordered, SDK emits schema order.
    preview = next((str(v) for v in inp.values() if isinstance(v, str) and v), "")
    preview = preview.replace("\n", " ").strip()
    if not preview:
        return f"{emoji} {display}"
    if len(preview) > 40:
        preview = preview[:40] + "..."
    return f'{emoji} {display}: "{preview}"'


def _to_html(md: str) -> str:
    """Best-effort markdown → TG HTML (the tags TG actually accepts).

    TG's HTML parse_mode ONLY supports: b/i/u/s, code, pre (+ language via
    nested code class), a, blockquote, tg-spoiler. Headings / lists / tables /
    hr are NOT real tags — they must degrade to something TG renders.

    Strategy:
      1. Preserve code blocks, heading-as-bold, blockquote, links as opaque
         placeholders BEFORE html.escape so their inner content escapes
         correctly and outer regexes don't mangle them.
      2. html.escape the remaining plain text so stray <>& become entities.
      3. Run inline replacements on escaped text (backtick / ** / * / ~~).
      4. Restore placeholders.
    """
    if not md:
        return ""

    blocks: list[str] = []

    def _park(html_fragment: str) -> str:
        blocks.append(html_fragment)
        return f"\x00BLK{len(blocks) - 1}\x00"

    # 1a. Fenced code blocks (first — highest precedence, opaque)
    def _save_code(m: re.Match) -> str:
        lang = m.group(1) or ""
        code = html.escape(m.group(2))
        return _park(
            f'<pre><code class="language-{lang}">{code}</code></pre>' if lang
            else f"<pre>{code}</pre>"
        )
    text = re.sub(r"```(\w*)\n(.*?)```", _save_code, md, flags=re.DOTALL)

    # 1b. Pre-existing TG-compatible HTML tags that have no markdown equivalent.
    # Users / CC may write them directly; we park them so the later
    # html.escape doesn't turn `<u>` into `&lt;u&gt;`. Single-level only
    # (no nested tag parsing) — adequate for chat.
    for _raw_tag in ("u", "ins", "tg-spoiler"):
        def _make_raw_saver(t: str):
            def _save(m: re.Match) -> str:
                return _park(f"<{t}>{html.escape(m.group(1))}</{t}>")
            return _save
        text = re.sub(
            rf"<{_raw_tag}>([^<]*)</{_raw_tag}>",
            _make_raw_saver(_raw_tag),
            text,
        )

    # 1c. Inline backtick code — PARK (not just replace) so later bold/italic
    # regexes can't reach the `**` or `*` inside. Without this, `\`**粗**\``
    # gets turned into <code>**粗**</code>, then bold regex chews through the
    # <code> tag and produces <code><b>粗</b></code> which breaks TG parsing
    # entirely and falls back to plain text.
    def _save_inline_code(m: re.Match) -> str:
        inner = html.escape(m.group(1))
        return _park(f"<code>{inner}</code>")
    text = re.sub(r"`([^`\n]+)`", _save_inline_code, text)

    # 1c. Markdown headings `# / ## / ###...` — TG has no heading tag, degrade
    # to <b> on its own line. Single-line form: `^#+ whatever` until EOL.
    def _save_heading(m: re.Match) -> str:
        inner = html.escape(m.group(2).strip())
        return _park(f"<b>{inner}</b>")
    text = re.sub(r"(?m)^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", _save_heading, text)

    # 1d. Blockquote: consecutive `> line` merged into one <blockquote>
    def _save_bq(m: re.Match) -> str:
        raw = m.group(0)
        lines = [re.sub(r"^\s*>\s?", "", l) for l in raw.split("\n")]
        inner = html.escape("\n".join(lines).strip())
        return _park(f"<blockquote>{inner}</blockquote>")
    text = re.sub(r"(?m)(^[ \t]*>.*(?:\n[ \t]*>.*)*)", _save_bq, text)

    # 1e. Markdown links [text](url). Escape both pieces so user-provided `<`
    # can't break the HTML. href gets quote-escape too for the attribute.
    def _save_link(m: re.Match) -> str:
        label = html.escape(m.group(1))
        href = html.escape(m.group(2), quote=True)
        return _park(f'<a href="{href}">{label}</a>')
    text = re.sub(r"\[([^\]\n]+)\]\(([^)\n]+)\)", _save_link, text)

    # 2. Escape everything else
    text = html.escape(text)

    # 3. Inline emphasis on escaped text. All code/tag/link content is already
    # parked, so these regexes only touch real prose.
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)

    # 4. Restore placeholders (NUL + 'BLK' + digits survives html.escape intact)
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


async def cmd_verbose(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    labels = {0: "hidden", 1: "flash", 2: "keep"}
    buttons = [[
        InlineKeyboardButton(
            f"{'> ' if _verbose == i else ''}{v}",
            callback_data=f"verbose:{i}",
        ) for i, v in labels.items()
    ]]
    await update.message.reply_text(
        "Tool display:", reply_markup=InlineKeyboardMarkup(buttons),
    )


_ALIAS_WINDOW_RE = re.compile(r"\[(\d+)([mk])\]", re.I)


def _infer_window_from_alias(alias: str) -> int | None:
    """Parse CC model alias suffix: 'opus[1m]' → 1_000_000, '[200k]' → 200_000."""
    if not alias:
        return None
    m = _ALIAS_WINDOW_RE.search(alias)
    if not m:
        return None
    n = int(m.group(1))
    return n * 1_000_000 if m.group(2).lower() == "m" else n * 1_000


def _scan_recent_session_model() -> str | None:
    """Grep the most recent session JSONL for message.model. Gives resolved
    model name (e.g. 'claude-opus-4-N') even before this bot instance has run
    a turn, so /status after a restart shows something specific."""
    try:
        recent = cc._load_state().get("recent_sids") or []
    except Exception:
        recent = []
    proj_dir = Path.home() / ".claude/projects/-Users-admin"
    for sid in recent[:5]:
        fp = proj_dir / f"{sid}.jsonl"
        if not fp.is_file():
            continue
        try:
            for line in reversed(fp.read_text().splitlines()):
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                msg = d.get("message")
                if isinstance(msg, dict):
                    m = msg.get("model")
                    if isinstance(m, str) and m.startswith("claude-"):
                        return m
        except Exception:
            continue
    return None


def _cc_version() -> str:
    """Ask the actual claude binary for its version. Same binary bot spawns per
    query — so this number = what /new will run. Subprocess each call (low-
    frequency command, no cache needed)."""
    cli = os.environ.get("CLAUDE_CLI_PATH") or shutil.which("claude")
    if not cli:
        return "—"
    try:
        r = subprocess.run([cli, "-v"], capture_output=True, text=True, timeout=3)
        # "2.1.112 (Claude Code)" → "2.1.112"
        return r.stdout.strip().split()[0] if r.stdout else "—"
    except Exception:
        return "—"


def _sdk_version() -> str:
    """claude-agent-sdk version from this bot's own venv — the same package
    cc.py imports to spawn queries, so this = what /new will actually use."""
    try:
        import claude_agent_sdk  # already a dep of cc.py
        return getattr(claude_agent_sdk, "__version__", "—") or "—"
    except Exception:
        return "—"


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Show current model, context usage, cost, session state."""
    if not _allowed(update):
        return

    # Config-level model (what settings.json asks for — may be alias like "opus[1m]").
    # Differs from actual model name SDK reports (resolved full version).
    cfg_model = "—"
    try:
        cfg = json.loads((Path.home() / ".claude/settings.json").read_text())
        cfg_model = cfg.get("model", "—")
    except Exception:
        pass

    # Actual model resolution order:
    #   1. _last_model — set by this bot instance from ResultMessage.model_usage
    #      (full alias-suffixed form like 'claude-opus-4-N[1m]').
    #   2. Scan recent session JSONL (survives bot restart with no turns yet) —
    #      gives bare 'claude-opus-4-N'; re-attach cfg's [..] suffix so the
    #      displayed string stays consistent with what model_usage would show.
    #   3. Fall back to cfg alias — at least shows something concrete.
    actual = _last_model
    if not actual:
        scanned = _scan_recent_session_model()
        if scanned:
            suffix_match = _ALIAS_WINDOW_RE.search(cfg_model or "")
            actual = f"{scanned}{suffix_match.group(0)}" if suffix_match else scanned
    if not actual:
        actual = cfg_model

    # Context: prefer live numbers, else show window inferred from alias so V
    # at least sees the tier (—/1.0M) instead of a bare dash.
    win = _last_context_window or _infer_window_from_alias(cfg_model)
    if win:
        used = _last_used_tokens
        if used > 0:
            pct = used / win * 100
            ctx_line = f"{_fmt_tok(used)} / {_fmt_tok(win)} ({pct:.1f}%)"
        else:
            ctx_line = f"— / {_fmt_tok(win)}"
    else:
        ctx_line = "—"

    sids = cc._load_state().get("recent_sids") or []
    sid_now = cc._session_id[:8] if cc._session_id else "(new)"

    labels = {0: "hidden", 1: "flash", 2: "keep"}

    lines = [
        "<b>📊 Status</b>",
        "",
        f"<b>CC</b>        v{_cc_version()}",
        f"<b>SDK</b>       v{_sdk_version()}",
        f"<b>Model</b>     <code>{html.escape(actual)}</code>",
        f"<b>Session</b>   {sid_now} ({len(sids)} recent)",
        f"<b>Verbose</b>   {labels.get(_verbose, _verbose)}",
    ]
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


def _fmt_tok(n: int) -> str:
    """Human-readable token count: 34123 → 34.1K, 1000000 → 1.0M."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


async def cmd_context(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """TG /context — forwards CC CLI's own /context slash command output.

    /context is rendered locally by CC CLI (not the model), so passing it as a
    prompt through the SDK returns the authoritative breakdown in ResultMessage.
    Wrap in <pre> so TG preserves the table alignment."""
    if not _allowed(update):
        return

    wait_msg = await update.message.reply_text("查询中…")
    _inflight_enter()
    try:
        resp = await cc.query("/context")
        text = (resp.content or "").strip()
        if not text:
            await wait_msg.edit_text("/context 返回空, 可能 session 还没建立")
            return
        body = f"<pre>{html.escape(text)}</pre>"
        # TG hard cap 4096 chars/message. /context can exceed when many MCP
        # tools are listed — split on blank lines to keep tables intact.
        if len(body) <= 4000:
            await wait_msg.edit_text(body, parse_mode="HTML")
            return
        await wait_msg.delete()
        chunks: list[str] = []
        cur = ""
        for para in text.split("\n\n"):
            piece = (cur + "\n\n" + para).strip() if cur else para
            if len(piece) > 3500:
                if cur:
                    chunks.append(cur)
                cur = para
            else:
                cur = piece
        if cur:
            chunks.append(cur)
        for chunk in chunks:
            await update.message.reply_text(
                f"<pre>{html.escape(chunk)}</pre>", parse_mode="HTML"
            )
    except Exception as e:
        await wait_msg.edit_text(f"/context 失败: {type(e).__name__}: {e}")
    finally:
        _inflight_exit()


async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Graceful restart: 等 in-flight CC 任务跑完再让 launchd (KeepAlive=true)
    重拉. 和 SIGTERM 走同一条 _graceful_shutdown 路径, 保证 V 手动 /restart
    不中断任务.

    ThrottleInterval=10s + ExitTimeOut=600s 在 plist 里设置, 最多等 10 分钟
    让任务结束, 超时才 SIGKILL. os._exit(0) 在 _graceful_shutdown 末尾触发.
    """
    if not _allowed(update):
        return
    if _in_flight > 0:
        await update.message.reply_text(
            f"🕐 /restart 已排队 · 等 {_in_flight} 个 CC 任务跑完后重启"
        )
    else:
        await update.message.reply_text("🔄 重启中… 10 秒后回来")
    # Fire-and-forget: 当前 handler 返回后 _graceful_shutdown 再接管退出.
    # 直接 await 会卡住当前 update 的 response 循环.
    asyncio.create_task(
        _graceful_shutdown(ctx.application, reason="收到 /restart")
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

    try:
        text = await transcribe_voice(path)
    except Exception as e:
        await update.message.reply_text(f"\u274c 转录失败: {e}")
        return
    finally:
        path.unlink(missing_ok=True)

    await update.message.reply_text(f"\U0001f3a4 {text}")
    await _process(update, ctx, text)


# Physical: TG albums (multi-photo messages) arrive as N separate Updates
# tied by media_group_id. Without coalescing, each photo fires its own CC
# query and the album appears to CC as N independent single-photo messages.
_MEDIA_GROUP_DEBOUNCE = 1.0
_media_groups: dict[str, dict] = {}


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

    image = image_to_base64(path)
    caption = update.message.caption or ""
    gid = update.message.media_group_id

    if not gid:
        prompt = f"[图片: {path}]\n{caption}" if caption else f"[图片: {path}]"
        await _process(update, ctx, prompt, images=[image])
        return

    # Single-threaded asyncio: dict mutation without await is atomic, no lock needed.
    group = _media_groups.get(gid)
    start_flush = group is None
    if start_flush:
        group = {
            "images": [],
            "paths": [],
            "captions": [],
            "first_update": update,
        }
        _media_groups[gid] = group
    group["images"].append(image)
    group["paths"].append(path)
    if caption:
        group["captions"].append(caption)
    group["last_update_at"] = time.monotonic()

    if start_flush:
        asyncio.create_task(_flush_media_group(gid, ctx))


async def _flush_media_group(gid: str, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Wait until the album stops growing for _MEDIA_GROUP_DEBOUNCE seconds, then flush once."""
    while True:
        await asyncio.sleep(_MEDIA_GROUP_DEBOUNCE)
        group = _media_groups.get(gid)
        if group is None:
            return
        if time.monotonic() - group["last_update_at"] < _MEDIA_GROUP_DEBOUNCE:
            continue
        _media_groups.pop(gid, None)
        break

    images = group["images"]
    paths = group["paths"]
    captions = group["captions"]
    first_update = group["first_update"]

    paths_str = ", ".join(str(p) for p in paths)
    header = f"[图片 ×{len(images)}: {paths_str}]" if len(images) > 1 else f"[图片: {paths_str}]"
    caption_text = "\n".join(captions)
    prompt = f"{header}\n{caption_text}" if caption_text else header

    try:
        await _process(first_update, ctx, prompt, images=images)
    except Exception as e:
        log.error("Media group flush failed (gid=%s): %s", gid, e)


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

    prompt = f"{caption}\n\n[Video summary (mimo-v2-omni)]: {summary}" if caption else f"[Video summary (mimo-v2-omni)]: {summary}"
    await _process(update, ctx, prompt)


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

    caption = update.message.caption or f"[received file: {save_path}]"
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


# ── /resume (pick a past session to continue) ────────────────────────

def _fmt_ago(ts: float) -> str:
    """Relative-age label for session picker. mtime is already local clock."""
    if not ts:
        return "?"
    dt = time.time() - ts
    if dt < 0:
        return "now"
    if dt < 60:
        return f"{int(dt)}s"
    if dt < 3600:
        return f"{int(dt / 60)}m"
    if dt < 86400:
        return f"{int(dt / 3600)}h"
    return f"{int(dt / 86400)}d"


# 渠道 category → cc.py 的 channel label 白名单. 扩展新 channel 时只在此处维护.
# cc.py 层只认 channel label (巴巴塔 / 巴巴塔2 / wx / term / oneshot / ...),
# 不知 category 概念.
#
# "当前" = 当前 bot 实例自己的 session (按 BABATA_INSTANCE 从 INSTANCE_LABELS
# 取昵称). 每个 bot 进程只看得见自己的 TG 历史, 不跨 bot 混显 — 所以 session 条
# 目上也不需要 [巴巴塔N] 前缀 tag.
#
# "终端" (cli entrypoint) vs "一次性" (sdk-cli = claude -p) 分开, 避免 cron
# 的一次性 session 把 bb 交互列表塞满. 判定在 cc.list_recent_sessions 按 JSONL
# entrypoint 字段打 label.
_CURRENT_LABEL = INSTANCE_LABELS.get(INSTANCE, INSTANCE or PROJECT)
_RESUME_CATEGORIES: list[tuple[str, str, list[str]]] = [
    # (category_id, 中文显示名, channel labels in cc.py)
    ("tg",      "当前",   [_CURRENT_LABEL]),
    ("wx",      "微信",   [INSTANCE_LABELS["weixin"]]),
    ("term",    "终端",   ["term"]),
    ("oneshot", "一次性", ["oneshot"]),
]


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Two-level session picker.

    Level 1: 选渠道 (TG / 微信 / 终端 / 一次性). /resume 直接给的这层.
    Level 2: 选具体 session. 对应渠道内最近 5 条.

    两级设计避免跨渠道 session 混在一个 list 里噪音大, V 明确指定"在哪个渠道
    的历史里挑"让 picker 更聚焦. 仍然跨渠道可见 — 在 TG 里也能看到终端 /微信开的
    session, 只是需要先点对应按钮.
    """
    if not _allowed(update):
        return
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup

    buttons = [
        [InlineKeyboardButton(name, callback_data=f"resume-ch:{cat}")]
        for cat, name, _ in _RESUME_CATEGORIES
    ]
    cur = cc._session_id
    header = f"当前: {cur[:8]}\n选一个渠道:" if cur else "当前: (无)\n选一个渠道:"
    await update.message.reply_text(
        header, reply_markup=InlineKeyboardMarkup(buttons),
    )


async def on_resume_channel_pick(
    update: Update, ctx: ContextTypes.DEFAULT_TYPE,
) -> None:
    """Level-1 callback: 用户选了某个渠道类别, 列该类别最近 session."""
    query = update.callback_query
    await query.answer()
    data = query.data or ""
    if not data.startswith("resume-ch:"):
        return
    cat_id = data.split(":", 1)[1]

    cat = next((c for c in _RESUME_CATEGORIES if c[0] == cat_id), None)
    if not cat:
        try:
            await query.edit_message_text(f"❌ 未知渠道: {cat_id}")
        except Exception:
            pass
        return
    _, cat_name, channel_labels = cat

    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    sessions = cc.list_recent_sessions(limit=5, channel_filter=channel_labels)
    if not sessions:
        try:
            await query.edit_message_text(f"{cat_name}: 暂无历史 session")
        except Exception:
            pass
        return

    buttons = []
    for s in sessions:
        # preview 优先用 haiku 生成的一句话总结 (见 cc._spawn_summary_generation).
        # 首次缓存未命中时 fallback first_user, 下次 /resume 就能看见总结.
        preview = (s.get("preview") or s["first_user"]).replace("\n", " ").strip()
        if len(preview) > 48:
            preview = preview[:48] + "…"
        marker = "● " if s["is_current"] else ""
        # 每个 category 都是单 channel 白名单 (当前 bot 自己 / wx / term / oneshot),
        # 不会混入其他渠道 session, 所以不再加 [昵称] 前缀 tag.
        label = f"{marker}{_fmt_ago(s['mtime'])} · {preview}"
        # callback_data hard cap is 64 bytes. "resume:" (7) + uuid (36) = 43 ✓
        buttons.append([
            InlineKeyboardButton(label, callback_data=f"resume:{s['sid']}"),
        ])
    try:
        await query.edit_message_text(
            f"{cat_name} 渠道最近 session, 选一个恢复:",
            reply_markup=InlineKeyboardMarkup(buttons),
        )
    except Exception:
        pass


async def on_resume_click(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Level-2 callback: 用户选了具体 session, 切换 cc 活动 session."""
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("resume:"):
        return
    sid = data.split(":", 1)[1]

    if not cc.resume(sid):
        try:
            await query.edit_message_text(f"❌ session {sid[:8]} 已失效 (JSONL 被清)")
        except Exception:
            pass
        return

    # Reset per-turn status accumulators so /status reflects the resumed
    # session's cost/model after its next turn, not the previous thread's
    # leftovers. Preserve _last_model/_last_context_window so /status before
    # the next turn still shows something concrete (scanner falls back to
    # JSONL anyway, but this avoids a misleading cost/tokens mismatch).
    global _session_cost, _session_turns, _last_used_tokens, _last_cost
    _session_cost = 0.0
    _session_turns = 0
    _last_used_tokens = 0
    _last_cost = 0.0
    _state["session_cost"] = 0.0
    _state["session_turns"] = 0
    _state["last_used_tokens"] = 0
    _state["last_cost"] = 0.0
    _save_state()

    # Show the last 2 rounds of the resumed session so V can recognize which
    # thread it is — picker's 48-char first-user preview is often ambiguous
    # between nearby sessions. Fall back to bare confirm if JSONL has no
    # text-bearing turns yet (fresh session, or all turns were tool-only).
    turns = cc.get_recent_turns(sid, pairs=2)
    if turns:
        blocks = []
        for role, text in turns:
            who = "V" if role == "user" else "CC"
            blocks.append(f"<b>{who}:</b> {html.escape(text)}")
        preview = "\n\n".join(blocks)
        body = (
            f"✅ 已恢复 <code>{sid[:8]}</code>\n\n"
            f"<blockquote>{preview}</blockquote>\n\n"
            "继续发消息即可。"
        )
        parse = "HTML"
    else:
        body = f"✅ 已恢复 {sid[:8]},继续发消息即可。"
        parse = None
    try:
        await query.edit_message_text(body, parse_mode=parse)
    except Exception:
        # HTML rejection (escaped something TG parser still dislikes) — retry plain
        try:
            await query.edit_message_text(f"✅ 已恢复 {sid[:8]},继续发消息即可。")
        except Exception:
            pass


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

    # Tool stream: lazy-created on first tool call, one line per tool.
    # Text stream: lazy-created on first text delta, edited in place as CC
    # generates. Two independent TG messages — no UI collision with tool status.
    tool_status = None
    entries: list[str] = []
    last_edit = 0.0

    # Text streaming state (text_chunk is a delta from StreamEvent; see cc.py)
    text_message = None
    text_buffer = ""
    text_last_edit = 0.0

    async def _on_stream(
        tool_name: str | None,
        tool_input: dict | None,
        text_chunk: str | None,
        tool_result: dict | None = None,
    ) -> None:
        nonlocal last_edit, tool_status
        nonlocal text_message, text_buffer, text_last_edit

        # Text delta stream → live-edit a TG message as CC generates
        if text_chunk is not None:
            text_buffer += text_chunk
            now = time.monotonic()
            # Plain text during streaming (HTML may have broken tags mid-stream).
            # When buffer exceeds TG cap, show the *tail* as a live-scrolling
            # preview; the final authoritative message is sent below as HTML.
            if len(text_buffer) <= _MAX_TG:
                display = text_buffer
            else:
                display = "\u2026" + text_buffer[-(_MAX_TG - 1):]

            if text_message is None:
                try:
                    text_message = await msg.reply_text(display or "\u2026")
                    text_last_edit = now
                except Exception:
                    pass
                return

            if now - text_last_edit < 2.0:
                return
            text_last_edit = now

            try:
                await text_message.edit_text(display)
            except Exception:
                pass  # FLOOD_WAIT / identical content / etc — skip
            try:
                await chat.send_action("typing")
            except Exception:
                pass
            return

        # Tool activity (existing behavior, gated by verbose)
        if _verbose == 0:
            return
        if tool_name:
            entries.append(_fmt_tool(tool_name, tool_input or {}))
        elif tool_result and tool_result.get("is_error"):
            # Surface real tool errors so CC can't hallucinate high-level reasons
            err = (tool_result.get("text") or "").replace("\n", " ").strip()
            if not err:
                return
            entries.append(f"  \u274c {err[:200]}")
        else:
            return

        body = "\n".join(entries[-30:])[:_MAX_TG]

        if tool_status is None:
            try:
                tool_status = await msg.reply_text(body)
            except Exception:
                return
            last_edit = time.monotonic()
            return

        now = time.monotonic()
        if now - last_edit < 2.0:
            return
        last_edit = now

        try:
            await tool_status.edit_text(body)
        except Exception:
            pass
        try:
            await chat.send_action("typing")
        except Exception:
            pass

    _inflight_enter()
    try:
        resp = await cc.query(text, images=images, on_stream=_on_stream)
    except Exception as e:
        log.error("CC query failed: %s", e)
        # Surface error on whichever live message is most visible
        surfaced = False
        for target in (text_message, tool_status):
            if target is None:
                continue
            try:
                await target.edit_text(f"Error: {e}")
                surfaced = True
                break
            except Exception:
                continue
        if not surfaced:
            await msg.reply_text(f"Error: {e}")
        return
    finally:
        _inflight_exit()

    # Tool stream: flash deletes, keep preserves.
    if tool_status and _verbose == 1:
        try:
            await tool_status.delete()
        except Exception:
            pass

    # Surface any session-resume note loud
    if resp.resume_note:
        try:
            await msg.reply_text(resp.resume_note)
        except Exception:
            pass

    if not resp.content:
        await msg.reply_text("(no response)")
        return

    html_text = _to_html(resp.content)
    parts = _split(html_text)

    # If streaming was active, promote the live text_message to the final
    # HTML-formatted first part (in place), then append remaining parts as
    # new messages. Avoids a duplicate "plain → formatted" double-send.
    if text_message and parts:
        try:
            await text_message.edit_text(parts[0], parse_mode="HTML")
        except Exception:
            # HTML broke mid-stream edit (TG parser rejected) — fall back to
            # raw content, same chunking
            try:
                raw_parts = _split(resp.content)
                await text_message.edit_text(raw_parts[0])
                for pp in raw_parts[1:]:
                    await msg.reply_text(pp)
                parts = []  # already delivered
            except Exception:
                pass
        for part in parts[1:]:
            try:
                await msg.reply_text(part, parse_mode="HTML")
            except Exception:
                await msg.reply_text(part)
    else:
        for part in parts:
            try:
                await msg.reply_text(part, parse_mode="HTML")
            except Exception:
                for pp in _split(resp.content):
                    await msg.reply_text(pp)
                break

    # Status accounting (populated by cc.py from ResultMessage.model_usage).
    # /new reset is detected by session_id going empty while cc.reset() ran.
    global _session_cost, _session_turns, _last_model, _last_context_window
    global _last_used_tokens, _last_cost
    if not resp.session_id and resp.cost == 0.0 and not resp.tools:
        # cc.reset() shortcut — wipe accumulators (keep _last_model: V wants
        # to know what model runs even right after /new, before next query)
        _session_cost = 0.0
        _session_turns = 0
        _last_used_tokens = 0
        _last_cost = 0.0
    else:
        _session_cost += resp.cost
        _session_turns += 1
        _last_cost = resp.cost
        if resp.model:
            _last_model = resp.model
        if resp.context_window:
            _last_context_window = resp.context_window
        # Context fill = what was sent to the model this turn.
        # Matches CC terminal's context indicator. Output is NOT part of context
        # (it's this turn's response; it becomes context only in the next turn's
        # input, at which point it shows up under input/cache_read).
        _last_used_tokens = (
            resp.input_tokens
            + resp.cache_creation_tokens
            + resp.cache_read_tokens
        )
    # Persist snapshot so /status survives bot restart
    _state["session_cost"] = _session_cost
    _state["session_turns"] = _session_turns
    _state["last_cost"] = _last_cost
    _state["last_used_tokens"] = _last_used_tokens
    if _last_model:
        _state["last_model"] = _last_model
    if _last_context_window:
        _state["last_context_window"] = _last_context_window
    _save_state()

    if resp.cost > 0:
        log.info("Cost: $%.4f | Session: %s", resp.cost, resp.session_id[:8] if resp.session_id else "new")


# ── Main ──────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    await bridge.start()
    asyncio.create_task(_heartbeat_loop(app))
    # Default context so terminal CC (no TG message yet) can push to user's TG
    if ALLOWED_USER:
        bridge.set_context(app.bot, ALLOWED_USER, None)
    # Graceful shutdown: 覆盖 PTB/asyncio 默认 signal handler, 等 in-flight
    # CC 跑完再退 (cmd_restart / launchd SIGTERM / Ctrl+C 都走这条).
    _install_signal_handlers(app)
    await app.bot.set_my_commands([
        ("new", "Start a fresh session"),
        ("resume", "Resume a recent session"),
        ("status", "Show model, session, verbose"),
        ("context", "Context usage breakdown"),
        ("verbose", "Tool display: 0=hidden 1=flash 2=keep"),
        ("restart", "Restart this bot process"),
    ])

    # 意外重启 / launchd kickstart / 任务中 /restart → bot 重连后主动告知 V 当
    # 前 session 号. 不走 hook (hook 只在 session 边界触发, bot 重启时 sid 没变).
    # sid 可能为 None (新进程还没跑过任何 CC session) — 显示 (new) 提示 V "接下来
    # 第一句话会开一个新 session".
    #
    # 孤儿 turn 告警: graceful shutdown 正常走完不会留孤儿; 但 SIGKILL / OOM /
    # 硬崩 绕过 graceful 时, CC CLI 被强杀来不及写完 assistant turn, jsonl 里会
    # 留一条 user 没回复. 检测到就附警告让 V 决定 /resume (看片段) 或 /new.
    if ALLOWED_USER:
        sid = cc._session_id
        sid_display = sid if sid else "(new)"
        lines = [f"[{_CURRENT_LABEL}] 上线 · session: {sid_display}"]
        if sid and cc.is_last_turn_orphan(sid):
            lines.append("⚠️ 上次 session 最后一条 user 无 assistant 回复 (可能 SIGKILL)")
        try:
            await app.bot.send_message(ALLOWED_USER, "\n".join(lines))
        except Exception as e:
            log.warning("startup notice send failed: %s", e)


def main() -> None:
    app = Application.builder().token(TOKEN).concurrent_updates(True).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("context", cmd_context))
    app.add_handler(CommandHandler("verbose", cmd_verbose))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler(["new", "reset"], on_text))
    app.add_handler(CallbackQueryHandler(on_verbose_click, pattern=r"^verbose:"))
    # resume-ch: 必须注册在 resume: 之前匹配更精确, 但 ^resume: 不会吞 ^resume-ch:
    # (第 7 个字符 '-' vs ':'), 两者 pattern 互斥, 顺序无关紧要.
    app.add_handler(CallbackQueryHandler(on_resume_channel_pick, pattern=r"^resume-ch:"))
    app.add_handler(CallbackQueryHandler(on_resume_click, pattern=r"^resume:"))
    app.add_handler(CallbackQueryHandler(on_button_click, pattern=r"^mcp:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VIDEO | filters.VIDEO_NOTE, on_video))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    log.info("Bot starting (user: %s)", ALLOWED_USER)
    # stop_signals=None: 禁用 PTB 默认 SIGTERM/SIGINT 立即停止逻辑; 我们在
    # _post_init 里装 _install_signal_handlers, 走 graceful drain 路径.
    app.run_polling(drop_pending_updates=True, stop_signals=None)


if __name__ == "__main__":
    main()
