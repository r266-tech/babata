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
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
from cc import Event, LiveSession, Response, VENV_PYTHON
from media import image_to_base64, transcribe_voice, understand_video

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ALLOWED_USER = int(os.environ.get("ALLOWED_USER_ID", "0"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")
log = logging.getLogger(PROJECT)

_TG_MCP_SCRIPT = str(Path(__file__).parent / "tg_mcp.py")

_TG_SOURCE_PROMPT = "Source: Telegram."

cc = LiveSession(
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
_channel_worker: "ChannelWorker | None" = None

# ── Graceful shutdown ─────────────────────────────────────────────────
# SIGTERM / SIGINT / /restart 都走 _graceful_shutdown: 若有 CC 任务在跑
# (_in_flight > 0), 先推 TG 告知, 等跑完再退. launchd plist 的 ExitTimeOut
# 必须调高 (默认 20s, babata 设 600s) 否则 SIGKILL 会强杀.
#
# Live worker 当前是否有 turn 在跑。PTB handler 现在只 enqueue; 真正需要
# graceful drain 的是后台 worker 从首条 user input 到 ResultMessage 的区间.
_in_flight = 0                  # active live CC turns
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
    """Wait for the live turn, notify V via TG, then exit."""
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

    if _channel_worker is not None:
        try:
            await _channel_worker.stop()
        except Exception as e:
            log.warning("Channel worker stop failed: %s", e)

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
# Today's cost — this bot instance only. Date-stamped so it self-resets across
# day rollover (instead of silently accumulating into yesterday's bucket).
_today_cost: float = float(_state.get("today_cost", 0.0))
_today_cost_date: str = _state.get("today_cost_date", "")

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


@dataclass
class Payload:
    update: Update
    ctx: ContextTypes.DEFAULT_TYPE
    text: str
    images: list[dict[str, str]] | None = None


class ChannelWorker:
    """Per-process TG channel worker for one long-lived LiveSession."""

    def __init__(self, session: LiveSession, *, instance_label: str) -> None:
        self.session = session
        self.instance_label = instance_label
        self._consume_task: asyncio.Task[None] | None = None
        self._state_lock = asyncio.Lock()
        self._turn_active = False
        self._turn_payload: Payload | None = None
        # P1.4: most-recent submitted payload; used as a fallback anchor when
        # _turn_payload was reset by turn_end before the next turn's events
        # land (race between submit() and _handle_turn_end acquiring _state_lock).
        self._latest_payload: Payload | None = None
        self._last_user_msg_id: int | None = None
        self._turn_anchor: int | None = None
        self._tool_status: Any | None = None
        self._tool_entries: list[str] = []
        self._tool_last_edit = 0.0
        self._text_message: Any | None = None
        self._text_buffer = ""
        self._text_last_edit = 0.0
        self._stopping = False  # set on graceful shutdown to break supervisor loop
        # 消息状态 reaction: 👀 = SDK 开始处理这条 / 👌 = 这条触发的 turn 已结束.
        # _pending_marks: submit 后等下个 _begin_turn 接管 (push 到 active_marks 并打 👀)
        # _active_marks: 当前 turn 已 picked_up; turn_end 时打 👌 并清空
        # 每条 mark = (bot, chat_id, message_id). bot 用 Any 因为 PTB Bot 实例 (含 ctx.bot)
        self._pending_marks: list[tuple[Any, int, int]] = []
        self._active_marks: list[tuple[Any, int, int]] = []
        self._reaction_tasks: set[asyncio.Task[None]] = set()
        # P2-A: 串行所有 reaction API 调用, 保证 schedule 顺序 = 执行顺序
        # (turn_end finally 先 schedule 👌 再触发 _begin_turn → 👀, lock FIFO
        # 保证 V 看到的最终 reaction 是 👌 不是 👀).
        self._reaction_lock = asyncio.Lock()
        # P1-A/B: anchor generation token. submit / _begin_turn 切 anchor 时 +1;
        # _handle_text_delta / _handle_tool_event 在 await 边界检查, 变了就 abort
        # 防 stale write 覆盖新 anchor 状态.
        self._anchor_generation: int = 0
        # 流式输出 reply 的 anchor — 跟 _turn_payload 解耦.
        # _turn_payload 是 SDK turn 边界 anchor (P1.4 promote 用); 一个 turn 可能
        # 跨多条 V 消息. _active_reply_payload 是 V 视角的 "当前活跃消息" — 每条
        # V message 进来都切到自己, 让流式输出和 tool 状态走新 reply, 不混到上
        # 一条的 reply 上 (即使 SDK 把多条 batch 成一个 turn 也能保持 per-message
        # reply 体验).
        self._active_reply_payload: Payload | None = None

    async def start(self) -> None:
        await self.session.connect()
        self._consume_task = asyncio.create_task(self._consume_events())

    async def stop(self) -> None:
        self._stopping = True
        await self.session.close()
        if self._consume_task:
            try:
                await asyncio.wait_for(self._consume_task, timeout=5)
            except asyncio.TimeoutError:
                self._consume_task.cancel()
                try:
                    await self._consume_task
                except asyncio.CancelledError:
                    pass
        # 等 in-flight reaction tasks 跑完 (避免 asyncio Task was destroyed warning).
        # 短超时: reaction 是 fire-and-forget, 卡了直接放弃.
        if self._reaction_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._reaction_tasks, return_exceptions=True),
                    timeout=2,
                )
            except (asyncio.TimeoutError, Exception):
                pass

    async def submit(self, payload: Payload) -> None:
        chat = payload.update.effective_chat
        msg = payload.update.effective_message
        if chat is None or msg is None:
            return

        bridge.set_context(payload.ctx.bot, chat.id, msg.message_id)
        self._last_user_msg_id = msg.message_id

        async with self._state_lock:
            if payload.text.strip() in ("/new", "/reset") and not payload.images:
                await self._handle_reset(payload)
                return

            # P1.4: always update latest_payload so a mid-turn submit (which
            # doesn't trigger _begin_turn) still leaves a valid anchor for the
            # next SDK turn if the race between submit and turn_end leaves
            # _turn_payload unset.
            self._latest_payload = payload
            # 消息状态: 入 _pending_marks 队列, 等 _begin_turn 接管时一起 fire 👀
            self._pending_marks.append((payload.ctx.bot, chat.id, msg.message_id))
            # 切流式输出 anchor 到新消息: 后续 text_delta / tool_event 会 reply
            # 到这条 V 消息, 不接前一条 reply. 上一条 reply 停在最后流式状态
            # (V 看到的是 msg1 reply 中途断, msg2 reply 续上后续内容).
            # P1-A/B: anchor_generation += 1 让 in-flight text_delta/tool_event
            # await 醒来后检测到 stale → abort, 不污染新 anchor 状态.
            self._anchor_generation += 1
            self._text_message = None
            self._text_buffer = ""
            self._text_last_edit = 0.0
            self._tool_status = None
            self._tool_entries = []
            self._tool_last_edit = 0.0
            self._active_reply_payload = payload
            if not self._turn_active:
                self._begin_turn(payload)

            try:
                self.session.submit(payload.text, payload.images)
            except RuntimeError:
                log.warning("LiveSession was disconnected; reconnecting before submit")
                try:
                    await self.session.connect()
                    self.session.submit(payload.text, payload.images)
                except Exception as e:
                    # MINOR-1 fix: 二次失败 silent loss + in_flight 卡死 →
                    # 回滚 (fire 💔 给已 push 的 mark + reset turn state),
                    # V 通过 reaction 看到这条 V msg 没成功.
                    log.error(
                        "Second submit failed: %s — dropping V message", e
                    )
                    self._reset_turn_state(
                        exit_inflight=True,
                        drop_pending=True,
                        fail_emoji="💔",
                    )

    async def interrupt(self) -> None:
        await self.session.interrupt()

    async def resume(self, sid: str) -> bool:
        async with self._state_lock:
            if self._turn_active:
                await self._surface_error(RuntimeError("会话已切换。"))
            # P2-D: drop_pending — resume 后 inbox drain (LiveSession.resume_live
            # 内部 _stop_client_locked 会 drain), pending V messages 永远不会被
            # 处理, 不能让它们的 mark 被下次 _begin_turn promote.
            # 💔: 让 V 看到这些 V msg 被中止, reaction 状态不卡 👀.
            self._reset_turn_state(
                exit_inflight=True, drop_pending=True, fail_emoji="💔"
            )
            return await self.session.resume_live(sid)

    async def _handle_reset(self, payload: Payload) -> None:
        if self._turn_active:
            await self._surface_error(RuntimeError("会话已重置。"))
        # P2-D: 同 resume — /new drain 后 pending marks 失效.
        # 💔: 标记这些 V msg 为"未完成" (区别于 turn_end 的 👌).
        self._reset_turn_state(
            exit_inflight=True, drop_pending=True, fail_emoji="💔"
        )
        resp = await self.session.reset_live()
        await self._deliver_response(payload, resp)
        self._apply_accounting(resp)

    def _begin_turn(self, payload: Payload) -> None:
        msg = payload.update.effective_message
        self._turn_active = True
        self._turn_payload = payload
        self._latest_payload = payload  # keep latest in sync
        # P1-A/B: 切 anchor 时 +1 generation. P1.4 promote 路径走这里, 也要 bump.
        self._anchor_generation += 1
        self._active_reply_payload = payload  # 同步切 reply anchor
        self._turn_anchor = msg.message_id if msg else None
        self._tool_status = None
        self._tool_entries = []
        self._tool_last_edit = 0.0
        self._text_message = None
        self._text_buffer = ""
        self._text_last_edit = 0.0
        # 消息状态: pending → active, 给本 turn 覆盖的所有 V message 打 👀
        # (单 turn 可能聚合多条 message: 第一条立即 _begin_turn 时 pending=[m1];
        # 其后 turn 结束 P1.4 promote 路径再 _begin_turn 时 pending 可能 [m2, m3, ...])
        if self._pending_marks:
            self._active_marks = self._pending_marks
            self._pending_marks = []
            self._schedule_marks(self._active_marks, "👀")
        _inflight_enter()

    async def _consume_events(self) -> None:
        """P1.2 supervisor: re-establish the LiveSession when events() exits
        on un-recovered error so V's next message still gets processed instead
        of vanishing into a dead inbox.
        """
        backoff = 1.0
        while not self._stopping:
            try:
                async for ev in self.session.events():
                    if ev.kind == "text_delta":
                        await self._handle_text_delta(ev.chunk or "")
                    elif ev.kind in ("tool_use", "tool_result"):
                        await self._handle_tool_event(ev)
                    elif ev.kind == "turn_end" and ev.response:
                        await self._handle_turn_end(ev.response)
                    elif ev.kind == "session_changed":
                        log.info(
                            "Session changed: %s -> %s",
                            (ev.old_sid or "")[:8],
                            (ev.new_sid or "")[:8],
                        )
                    elif ev.kind == "error":
                        await self._handle_error(
                            ev.exception or RuntimeError("CC stream error")
                        )
                        break  # events() will exit after error; supervisor reconnects
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("ChannelWorker consume loop crashed: %s", e)
                # Make sure turn-bound state doesn't leak across reconnect
                async with self._state_lock:
                    if self._turn_active:
                        await self._surface_error(e)
                        # P2-D: consume crash → reconnect, pending V msgs 失效
                        # 💔 让 V 一眼看到这些没完成.
                        self._reset_turn_state(
                            exit_inflight=True,
                            drop_pending=True,
                            fail_emoji="💔",
                        )

            if self._stopping:
                return
            # Reconnect with backoff. session is marked closed after un-recovered
            # error, so connect() will start a fresh CLI subprocess.
            try:
                await self.session.connect()
                backoff = 1.0
            except Exception as e:
                log.warning("LiveSession reconnect failed: %s; retry in %.1fs", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _handle_text_delta(self, chunk: str) -> None:
        if not chunk:
            return
        # 优先用 _active_reply_payload (V 视角"当前活跃消息" — 每条 V msg 都切到
        # 自己, 即使 SDK 还没 turn_end 也能让新消息有独立 reply).
        # P1.4 fallback: turn_payload / latest_payload 兜底 (race 窗口).
        # P1-A: snapshot generation 入口, 每次 await 边界检查; 变了就 abort
        # 避免 stale write 覆盖新 anchor 状态.
        gen = self._anchor_generation
        payload = (
            self._active_reply_payload
            or self._turn_payload
            or self._latest_payload
        )
        if payload is None:
            return
        chat = payload.update.effective_chat
        msg = payload.update.effective_message
        if chat is None or msg is None:
            return

        self._text_buffer += chunk
        now = time.monotonic()
        if len(self._text_buffer) <= _MAX_TG:
            display = self._text_buffer
        else:
            display = "…" + self._text_buffer[-(_MAX_TG - 1):]

        if self._text_message is None:
            try:
                new_reply = await msg.reply_text(display or "…")
            except Exception:
                return
            # P1-A: gen 没变才装回 _text_message. 变了说明 submit 已经切到新
            # anchor, 这条 reply 是孤儿 (V 看到一条 "…" 或 first chunk 的孤立
            # reply), 不能装回去覆盖新 anchor 的 _text_message=None 状态.
            if gen == self._anchor_generation:
                self._text_message = new_reply
                self._text_last_edit = now
            return

        if now - self._text_last_edit < 2.0:
            return
        # P1-A: 在 edit await 前再查一次 gen — 变了就 abort, 不把新 chunk
        # 写到旧 reply (chunk 在 V 视角属于新 anchor).
        if gen != self._anchor_generation:
            return
        self._text_last_edit = now
        target = self._text_message
        try:
            await target.edit_text(display)
        except Exception:
            pass
        try:
            await chat.send_action("typing")
        except Exception:
            pass

    async def _handle_tool_event(self, ev: Event) -> None:
        if _verbose == 0:
            return
        # 同 _handle_text_delta: per-message reply anchor 优先, 让 tool 状态也
        # 跟着新 V 消息走 (不混到上一条 reply 链).
        # P1-B: gen check 同 _handle_text_delta.
        gen = self._anchor_generation
        payload = (
            self._active_reply_payload
            or self._turn_payload
            or self._latest_payload
        )
        if payload is None:
            return
        chat = payload.update.effective_chat
        msg = payload.update.effective_message
        if chat is None or msg is None:
            return

        if ev.kind == "tool_use" and ev.name:
            self._tool_entries.append(_fmt_tool(ev.name, ev.input_dict or {}))
        elif ev.kind == "tool_result" and ev.is_error:
            err = (ev.text or "").replace("\n", " ").strip()
            if not err:
                return
            self._tool_entries.append(f"  ❌ {err[:200]}")
        else:
            return

        body = "\n".join(self._tool_entries[-30:])[:_MAX_TG]
        if self._tool_status is None:
            try:
                new_status = await msg.reply_text(body)
            except Exception:
                return
            if gen == self._anchor_generation:
                self._tool_status = new_status
                self._tool_last_edit = time.monotonic()
            return

        now = time.monotonic()
        if now - self._tool_last_edit < 2.0:
            return
        if gen != self._anchor_generation:
            return
        self._tool_last_edit = now
        target = self._tool_status
        try:
            await target.edit_text(body)
        except Exception:
            pass
        try:
            await chat.send_action("typing")
        except Exception:
            pass

    async def _handle_turn_end(self, resp: Response) -> None:
        async with self._state_lock:
            # P1.4: race window — if submit() acquired _state_lock between SDK's
            # ResultMessage and consume_events reaching here, _turn_payload may
            # already point at a newer Payload. Either way, fall back to
            # _latest_payload so V never sees a silent drop.
            # P1-C/D: 优先用 _active_reply_payload (V 视角"当前活跃") — final
            # response 落到 V 最新 message 的 reply, 跟流式期间的 _text_message
            # 同 anchor; 否则 long-response overflow parts 会跨两个 anchor 分裂.
            payload = (
                self._active_reply_payload
                or self._turn_payload
                or self._latest_payload
            )
            # P1.3: try/finally guarantees turn state resets even if TG edits
            # raise — otherwise _in_flight stays >0 and graceful shutdown hangs.
            try:
                if payload is None:
                    log.warning(
                        "turn_end without any payload anchor: sid=%s", resp.session_id
                    )
                    self._apply_accounting(resp)
                    return
                try:
                    await self._deliver_response(payload, resp)
                except Exception as e:
                    log.exception("deliver_response failed: %s", e)
                self._apply_accounting(resp)
                if resp.cost > 0:
                    log.info(
                        "Cost: $%.4f | Session: %s",
                        resp.cost,
                        resp.session_id[:8] if resp.session_id else "new",
                    )
            finally:
                # SDK 在 V 快速连发时会把多条 V msg batch 成一个 turn (实测:
                # bot 一个 reply 同时回 m1+m2). 这种情况下 turn_end 处理了所有
                # 累积 V msg, 给它们都 fire 👌. per-msg case (理想 SDK 行为)
                # pending=[], active=[m_current] — 也兼容.
                all_done = self._active_marks + self._pending_marks
                self._active_marks = []
                self._pending_marks = []
                if all_done:
                    self._schedule_marks(all_done, "👌")
                self._reset_turn_state(exit_inflight=True)
                # 不再 P1.4 promote — batch 模式 SDK 不会发第二个 turn_end,
                # promote 会让 in_flight 永久卡死. per-msg 模式后续 SDK ev
                # 通过 _handle_text_delta 的 _latest_payload fallback 渲染.

    async def _handle_error(self, exc: Exception) -> None:
        log.error("CC stream failed: %s", exc)
        async with self._state_lock:
            await self._surface_error(exc)
            # P2-D: stream error → LiveSession 已 _mark_dead_after_error 清 inbox,
            # pending marks 永远不会被处理. ChannelWorker supervisor reconnect
            # 后 _begin_turn 不能 promote 这些已废弃 mark.
            # 💔 让 V 看到这些 V msg 因 error 中止 (跟 _surface_error 的 "Error:"
            # message 互补 — message 给具体原因, reaction 给状态).
            self._reset_turn_state(
                exit_inflight=True, drop_pending=True, fail_emoji="💔"
            )

    async def _surface_error(self, exc: Exception) -> None:
        text = f"Error: {exc}"
        surfaced = False
        for target in (self._text_message, self._tool_status):
            if target is None:
                continue
            try:
                await target.edit_text(text)
                surfaced = True
                break
            except Exception:
                continue
        if surfaced:
            return
        # P2-C: 用 _active_reply_payload 优先 (errors 应落 V 最新 message reply,
        # 不是旧 turn anchor).
        payload = (
            self._active_reply_payload
            or self._turn_payload
            or self._latest_payload
        )
        if payload and payload.update.effective_message:
            try:
                await payload.update.effective_message.reply_text(text)
            except Exception:
                pass

    async def _deliver_response(self, payload: Payload, resp: Response) -> None:
        msg = payload.update.effective_message
        if msg is None:
            return

        if self._tool_status and _verbose == 1:
            try:
                await self._tool_status.delete()
            except Exception:
                pass

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
        if self._text_message and parts:
            try:
                await self._text_message.edit_text(parts[0], parse_mode="HTML")
            except Exception:
                try:
                    raw_parts = _split(resp.content)
                    await self._text_message.edit_text(raw_parts[0])
                    for pp in raw_parts[1:]:
                        await msg.reply_text(pp)
                    parts = []
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

    def _apply_accounting(self, resp: Response) -> None:
        global _session_cost, _session_turns, _last_model, _last_context_window
        global _last_used_tokens, _last_cost, _today_cost
        if not resp.session_id and resp.cost == 0.0 and not resp.tools:
            # /new shortcut — wipe per-session accumulators. _today_cost
            # intentionally NOT reset: /new is a session boundary but today's
            # spending is per-calendar-day.
            _session_cost = 0.0
            _session_turns = 0
            _last_used_tokens = 0
            _last_cost = 0.0
        else:
            _session_cost += resp.cost
            _session_turns += 1
            _last_cost = resp.cost
            _roll_today_cost(datetime.now().strftime("%Y-%m-%d"))
            _today_cost += resp.cost
            if resp.model:
                _last_model = resp.model
            if resp.context_window:
                _last_context_window = resp.context_window
            _last_used_tokens = (
                resp.input_tokens
                + resp.cache_creation_tokens
                + resp.cache_read_tokens
            )

        _state["session_cost"] = _session_cost
        _state["session_turns"] = _session_turns
        _state["last_cost"] = _last_cost
        _state["last_used_tokens"] = _last_used_tokens
        _state["today_cost"] = _today_cost
        _state["today_cost_date"] = _today_cost_date
        if _last_model:
            _state["last_model"] = _last_model
        if _last_context_window:
            _state["last_context_window"] = _last_context_window
        _save_state()

    def _reset_turn_state(
        self,
        *,
        exit_inflight: bool,
        drop_pending: bool = False,
        fail_emoji: str | None = None,
    ) -> None:
        was_active = self._turn_active
        self._turn_active = False
        self._turn_payload = None
        self._turn_anchor = None
        self._tool_status = None
        self._tool_entries = []
        self._tool_last_edit = 0.0
        self._text_message = None
        self._text_buffer = ""
        self._text_last_edit = 0.0
        # 失败路径 (error / reset / resume / submit retry fail): fire 💔 给
        # active + (drop 模式下) pending 让 V 一眼看到这些 V message 没正常完成.
        # turn_end 路径不传 fail_emoji — finally 段已经手动 fire 过 👌.
        if fail_emoji:
            failed = list(self._active_marks)
            if drop_pending:
                failed.extend(self._pending_marks)
            if failed:
                self._schedule_marks(failed, fail_emoji)
        self._active_marks = []
        self._active_reply_payload = None
        # P1-A/B: bump generation 让 in-flight handler 看到 stale.
        self._anchor_generation += 1
        # P2-D: reset/error/resume 路径要清 _pending_marks (这些 mark 的 V 消息
        # 永远不会被处理了 — /new 后 inbox 已 drain). turn_end 路径不清, 留给
        # P1.4 promote 给下个 SDK turn 用.
        if drop_pending:
            self._pending_marks = []
        if exit_inflight and was_active:
            _inflight_exit()

    def _schedule_marks(self, marks: list[tuple[Any, int, int]], emoji: str) -> None:
        """Fire-and-forget: 给一组 (bot, chat_id, msg_id) 打 reaction emoji."""
        if not marks:
            return
        task = asyncio.create_task(self._fire_marks(list(marks), emoji))
        self._reaction_tasks.add(task)
        task.add_done_callback(self._reaction_tasks.discard)

    async def _fire_marks(
        self,
        marks: list[tuple[Any, int, int]],
        emoji: str,
    ) -> None:
        # P2-A: 全局串行所有 reaction API 调用. asyncio.Lock FIFO 保证 schedule
        # 顺序 = 执行顺序 — turn_end finally 先 schedule 👌(active) 再触发
        # _begin_turn → 👀(next), V 看到的最终 reaction 一定是后者覆盖前者.
        # 没这个 lock, 两个 create_task 并发可能让快的 👀 覆盖慢的 👌, 那条
        # 消息卡在 👀 永远不变 👌 (TG setMessageReaction 是 last-write-wins).
        async with self._reaction_lock:
            for bot_obj, chat_id, msg_id in marks:
                try:
                    await bot_obj.set_message_reaction(
                        chat_id=chat_id,
                        message_id=msg_id,
                        reaction=emoji,
                    )
                except Exception as e:
                    # TG API throttling / message too old / bot 无权限 — 不影响主流程
                    log.debug(
                        "set_message_reaction(%s) %s/%s failed: %s",
                        emoji, chat_id, msg_id, e,
                    )


def _worker() -> ChannelWorker:
    if _channel_worker is None:
        raise RuntimeError("ChannelWorker is not started")
    return _channel_worker


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


# ── Status: quota / today cost / formatting helpers ──────────────────
#
# V's terminal CC /status shows:
#   [bar] N% · <model> (<window> context)
#   session N% · resets Npm  |  week N% · resets Mon DD  |  $X today
#
# Mirror it on the bot. Quota comes from api.anthropic.com/api/oauth/usage
# (OAuth-scoped endpoint CC itself uses), which needs the account's OAuth
# access token (lives in macOS keychain, auto-refreshed by /login).
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


def _claude_oauth_token() -> str | None:
    """Pull the OAuth access token from keychain (same slot /login writes).
    Single source; don't mirror it to .env (see memory reference on token
    single-source). Returns None if keychain lookup fails."""
    try:
        p = subprocess.run(
            ["security", "find-generic-password",
             "-a", os.environ.get("USER", "admin"),
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=2,
        )
        if p.returncode != 0 or not p.stdout.strip():
            return None
        return json.loads(p.stdout).get("claudeAiOauth", {}).get("accessToken")
    except Exception:
        return None


def _fetch_usage_sync() -> dict | None:
    """GET /api/oauth/usage. Blocking; call via run_in_executor from async.
    Returns parsed JSON or None on any failure (wrong token / no net / 4xx)."""
    token = _claude_oauth_token()
    if not token:
        return None
    req = urllib.request.Request(
        _USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, Exception):
        return None


async def _fetch_usage() -> dict | None:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _fetch_usage_sync)


def _fmt_reset(iso_str: str | None) -> str:
    """ISO8601 UTC → local-time display.
    Same local calendar date → '9pm' (hour, 12h lowercase).
    Different date → 'Apr 24' (month + day)."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00")).astimezone()
    except Exception:
        return "—"
    now = datetime.now().astimezone()
    if dt.date() == now.date():
        h = dt.hour
        ampm = "am" if h < 12 else "pm"
        h12 = h % 12 or 12
        if dt.minute == 0:
            return f"{h12}{ampm}"
        return f"{h12}:{dt.minute:02d}{ampm}"
    return dt.strftime("%b %-d")


def _progress_bar(pct: float, width: int = 15) -> str:
    """Render a block progress bar. Uses █ (full) vs ░ (light) — solid contrast
    renders cleanly in TG's iOS system font; ▓ (medium shade) gets rendered as
    a noisy stipple pattern at display size and looks junk."""
    pct = max(0.0, min(100.0, pct))
    filled = int(round(pct / 100 * width))
    return "█" * filled + "░" * (width - filled)


_MODEL_RE = re.compile(r"claude-(\w+)-(\d+)-(\d+)", re.I)


def _short_model(full: str | None) -> str:
    """'claude-opus-4-7[1m]' → 'Opus 4.7'. Alias suffix is dropped (shown
    separately as context window)."""
    if not full:
        return "—"
    m = _MODEL_RE.search(full)
    if not m:
        return full
    name, maj, min_ = m.groups()
    return f"{name.capitalize()} {maj}.{min_}"


def _short_window(tokens: int | None) -> str:
    """1_000_000 → '1M' / 200_000 → '200K'."""
    if not tokens:
        return "—"
    if tokens >= 1_000_000:
        n = tokens / 1_000_000
        return f"{n:g}M"
    if tokens >= 1_000:
        return f"{tokens // 1_000}K"
    return str(tokens)


def _roll_today_cost(now_date: str) -> None:
    """Zero the daily accumulator if we crossed midnight since last turn.
    Called from both turn accumulation and /status display paths."""
    global _today_cost, _today_cost_date
    if _today_cost_date != now_date:
        _today_cost = 0.0
        _today_cost_date = now_date


def _last_prompt_tokens(sid: str | None) -> int:
    """Context tokens for the most recent API call — NOT the turn aggregate.

    ResultMessage.model_usage sums cache_read across every tool iteration in a
    turn; the same prompt cache can be re-read 5–10× per turn, so the aggregate
    balloons past the context window (seen: 205% of 1M) even though each single
    call sits at ~30%. The real context fill is the *last* assistant message's
    usage. Scan the session jsonl backward for the most recent assistant entry
    and sum its input + cache_read + cache_creation (matches CC terminal's bar).
    """
    if not sid:
        return 0
    projects = Path.home() / ".claude" / "projects"
    for fp in projects.glob(f"*/{sid}.jsonl"):
        try:
            lines = fp.read_text().splitlines()
        except Exception:
            continue
        for line in reversed(lines):
            if '"type":"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            usage = (d.get("message") or {}).get("usage") or {}
            if usage:
                return int(
                    (usage.get("input_tokens") or 0)
                    + (usage.get("cache_creation_input_tokens") or 0)
                    + (usage.get("cache_read_input_tokens") or 0)
                )
    return 0


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

    win = _last_context_window or _infer_window_from_alias(cfg_model)
    used = _last_prompt_tokens(cc._session_id) or _last_used_tokens
    pct_ctx = (used / win * 100) if (win and used > 0) else 0.0
    bar = _progress_bar(pct_ctx)
    model_short = _short_model(actual)
    window_short = _short_window(win)

    # Quota: five_hour + seven_day utilization + $today from api/oauth/usage.
    # Graceful fallback to "—" if OAuth/network fails.
    usage = await _fetch_usage()
    if usage:
        fh = usage.get("five_hour") or {}
        wk = usage.get("seven_day") or {}
        s_pct = fh.get("utilization")
        s_reset = _fmt_reset(fh.get("resets_at"))
        w_pct = wk.get("utilization")
        w_reset = _fmt_reset(wk.get("resets_at"))
        session_line = f"session {s_pct:.0f}% · resets {s_reset}" if s_pct is not None else "session —"
        week_line = f"week {w_pct:.0f}% · resets {w_reset}" if w_pct is not None else "week —"
    else:
        session_line, week_line = "session —", "week —"

    _roll_today_cost(datetime.now().strftime("%Y-%m-%d"))
    today_line = f"${_today_cost:.2f} today"

    sids = cc._load_state().get("recent_sids") or []
    sid_now = cc._session_id if cc._session_id else "(new)"

    labels = {0: "hidden", 1: "flash", 2: "keep"}

    # Layout: header two lines in default font (no <code> — TG renders ▓/░ as
    # noisy stipple inside code blocks). Quota broken into 3 separate lines so
    # mobile doesn't wrap awkwardly. Session UUID on its own line, same reason.
    lines = [
        "<b>📊 Status</b>",
        "",
        f"{bar} {pct_ctx:.0f}% · {html.escape(model_short)} ({window_short})",
        "",
        html.escape(session_line),
        html.escape(week_line),
        today_line,
        "",
        f"CC v{_cc_version()} · SDK v{_sdk_version()} · {labels.get(_verbose, _verbose)}",
        f"<code>{html.escape(actual)}</code>",
        f"<code>{sid_now}</code> · {len(sids)} recent",
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
    """TG /context — query the live SDK context-usage control API."""
    if not _allowed(update):
        return

    wait_msg = await update.message.reply_text("查询中…")
    try:
        usage = await cc.context_usage()
        total = int(usage.get("totalTokens") or 0)
        max_tokens = int(usage.get("maxTokens") or 0)
        pct = float(usage.get("percentage") or 0.0)
        model = str(usage.get("model") or "—")
        lines = [
            f"Model: {model}",
            f"Total: {_fmt_tok(total)} / {_fmt_tok(max_tokens)} ({pct:.1f}%)",
            "",
            "Categories:",
        ]
        for cat in usage.get("categories") or []:
            name = cat.get("name", "?")
            tokens = int(cat.get("tokens") or 0)
            lines.append(f"- {name}: {_fmt_tok(tokens)}")
        mcp_tools = usage.get("mcpTools") or []
        if mcp_tools:
            lines.extend(["", "MCP tools:"])
            for item in mcp_tools[:30]:
                name = item.get("name") or item.get("toolName") or "?"
                server = item.get("serverName") or item.get("server") or "?"
                tokens = int(item.get("tokens") or 0)
                loaded = "" if item.get("isLoaded", True) else " (deferred)"
                lines.append(f"- {server}/{name}: {_fmt_tok(tokens)}{loaded}")
        body = f"<pre>{html.escape(chr(10).join(lines))}</pre>"
        await wait_msg.edit_text(body[:4000], parse_mode="HTML")
    except Exception as e:
        await wait_msg.edit_text(f"/context 失败: {type(e).__name__}: {e}")


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


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _allowed(update):
        return
    try:
        await _worker().interrupt()
        await update.message.reply_text("⏸  当前 turn 已请求中断")
    except Exception as e:
        await update.message.reply_text(f"/stop 失败: {type(e).__name__}: {e}")


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

    try:
        resumed = await _worker().resume(sid)
    except Exception as e:
        try:
            await query.edit_message_text(f"❌ resume 失败: {type(e).__name__}: {e}")
        except Exception:
            pass
        return
    if not resumed:
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
    """Enqueue user input into the live CC session and return immediately."""
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

    try:
        await _worker().submit(Payload(update=update, ctx=ctx, text=text, images=images))
    except Exception as e:
        log.error("enqueue failed: %s", e)
        await msg.reply_text(f"Error: {e}")


# ── Main ──────────────────────────────────────────────────────────────

async def _post_init(app: Application) -> None:
    global _channel_worker
    await bridge.start()
    asyncio.create_task(_heartbeat_loop(app))
    # Default context so terminal CC (no TG message yet) can push to user's TG
    if ALLOWED_USER:
        bridge.set_context(app.bot, ALLOWED_USER, None)
    _channel_worker = ChannelWorker(cc, instance_label=_CURRENT_LABEL)
    await _channel_worker.start()
    # Graceful shutdown: 覆盖 PTB/asyncio 默认 signal handler, 等 live turn
    # 跑完再退 (cmd_restart / launchd SIGTERM / Ctrl+C 都走这条).
    _install_signal_handlers(app)
    await app.bot.set_my_commands([
        ("new", "Start a fresh session"),
        ("resume", "Resume a recent session"),
        ("status", "Show model, session, verbose"),
        ("context", "Context usage breakdown"),
        ("verbose", "Tool display: 0=hidden 1=flash 2=keep"),
        ("stop", "Interrupt current turn"),
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
    app.add_handler(CommandHandler("stop", cmd_stop))
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
