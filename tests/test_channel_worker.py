import asyncio
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SDK_SITE = next(iter((_REPO / ".venv/lib").glob("python*/site-packages")), None)
if _SDK_SITE:
    sys.path.insert(0, str(_SDK_SITE))
sys.path.insert(0, str(_REPO))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("ALLOWED_USER_ID", "0")

import bot
from cc import Event, Response


class FakeBridge:
    def __init__(self):
        self.contexts = []

    def set_context(self, bot_obj, chat_id, reply_to=None):
        self.contexts.append((bot_obj, chat_id, reply_to))


class FakeSentMessage:
    _next_id = 100

    def __init__(self, text: str):
        self.message_id = FakeSentMessage._next_id
        FakeSentMessage._next_id += 1
        self.text = text
        self.edits = []
        self.deleted = False

    async def edit_text(self, text: str, parse_mode=None, reply_markup=None):
        self.text = text
        self.edits.append((text, parse_mode, reply_markup))

    async def delete(self):
        self.deleted = True


class HtmlRejectingSentMessage(FakeSentMessage):
    async def edit_text(self, text: str, parse_mode=None, reply_markup=None):
        if parse_mode:
            raise RuntimeError("html rejected")
        await super().edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)


class RejectingSentMessage(FakeSentMessage):
    async def edit_text(self, text: str, parse_mode=None, reply_markup=None):
        raise RuntimeError("edit rejected")


class FakeMessage:
    def __init__(self, message_id: int, text: str = ""):
        self.message_id = message_id
        self.text = text
        self.caption = None
        self.reply_to_message = None
        self.document = None
        self.photo = None
        self.voice = None
        self.audio = None
        self.replies: list[FakeSentMessage] = []

    async def reply_text(self, text: str, parse_mode=None, reply_markup=None):
        msg = FakeSentMessage(text)
        msg.parse_mode = parse_mode
        msg.reply_markup = reply_markup
        self.replies.append(msg)
        return msg


class HtmlRejectingMessage(FakeMessage):
    async def reply_text(self, text: str, parse_mode=None, reply_markup=None):
        if parse_mode:
            raise RuntimeError("html rejected")
        return await super().reply_text(text, parse_mode=parse_mode, reply_markup=reply_markup)


class FakeChat:
    def __init__(self, chat_id: int = 42):
        self.id = chat_id
        self.actions = []

    async def send_action(self, action: str):
        self.actions.append(action)


class FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class FakeUpdate:
    def __init__(
        self,
        message: FakeMessage,
        chat: FakeChat,
        user_id: int | None = None,
        update_id: int | None = None,
    ):
        self.update_id = update_id
        self.effective_message = message
        self.message = message
        self.effective_chat = chat
        self.effective_user = FakeUser(user_id) if user_id is not None else None


class FakeCallbackQuery:
    def __init__(self, user_id: int, data: str):
        self.from_user = FakeUser(user_id)
        self.data = data
        self.answers = []
        self.edits = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeCallbackUpdate:
    def __init__(self, query: FakeCallbackQuery):
        self.callback_query = query


class FakeBot:
    """PTB Bot stub. Records set_message_reaction calls so tests can assert
    👀 fired at turn-begin and 👌 at turn-end."""

    def __init__(self):
        self.reactions: list[tuple[int, int, str]] = []
        self.commands: list[tuple[str, str]] = []
        self.sent_messages: list[dict] = []
        self.chat_actions: list[tuple[int, str]] = []

    async def set_message_reaction(self, *, chat_id, message_id, reaction):
        self.reactions.append((chat_id, message_id, reaction))

    async def set_my_commands(self, commands):
        self.commands = list(commands)

    async def send_chat_action(self, *, chat_id, action):
        self.chat_actions.append((chat_id, action))

    async def send_message(self, **kwargs):
        self.sent_messages.append(kwargs)
        return FakeSentMessage(kwargs["text"])


class FakeCtx:
    def __init__(self):
        self.bot = FakeBot()
        self.application = object()


class FakeSession:
    def __init__(self):
        self.connected = False
        self.closed = False
        self.interrupted = False
        self.submitted = []
        self.queue: asyncio.Queue = asyncio.Queue()

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True
        self.queue.put_nowait(None)

    def submit(self, text, images=None):
        self.submitted.append((text, images))

    async def interrupt(self):
        self.interrupted = True

    async def resume_live(self, sid: str):
        self.resumed = sid
        return True

    async def reset_live(self):
        self.reset = True
        return Response(content="会话已重置。", session_id="", cost=0.0)

    async def events(self):
        while True:
            ev = await self.queue.get()
            if ev is None:
                return
            yield ev


async def wait_for(predicate, timeout: float = 1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for predicate")


def reset_bot_globals(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "bridge", FakeBridge())
    monkeypatch.setattr(bot, "_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(bot, "PROCESSED_UPDATES_FILE", tmp_path / "processed.json")
    monkeypatch.setattr(bot, "PENDING_UPDATES_FILE", tmp_path / "pending.json")
    monkeypatch.setattr(bot, "PENDING_DELIVERIES_FILE", tmp_path / "pending-deliveries.json")
    monkeypatch.setattr(bot, "RUNTIME_STATUS_FILE", tmp_path / "runtime.json")
    bot._processed_set = set()
    bot._pending_update_records = {}
    bot._pending_delivery_records = {}
    bot._state = {}
    bot._verbose = 1
    bot._in_flight = 0
    bot._session_cost = 0.0
    bot._session_turns = 0
    bot._last_model = None
    bot._last_context_window = None
    bot._last_used_tokens = 0
    bot._last_cost = 0.0
    bot._channel_worker = None
    bot._shutdown_requested = False


class FakeCpuSession:
    def __init__(self, name: str, state_file: Path | None = None, sid: str | None = None):
        self._babata_engine_name = name
        self._state_file = state_file
        self._session_id = sid

    def _load_state(self):
        if self._state_file is None:
            return {}
        try:
            return json.loads(self._state_file.read_text())
        except Exception:
            return {}

    @property
    def session_id(self) -> str | None:
        return self._session_id

    @property
    def assistant_engine_name(self) -> str:
        return self._babata_engine_name

    def recent_session_ids(self) -> list[str]:
        return list(self._load_state().get("recent_sids") or [])

    def persist_current_session(self):
        if self._state_file is None:
            return
        try:
            state = json.loads(self._state_file.read_text())
        except Exception:
            state = {}
        state["session_id"] = self._session_id
        engine_sids = state.get("engine_session_ids")
        if not isinstance(engine_sids, dict):
            engine_sids = {}
        engine_sids[self._babata_engine_name] = self._session_id or ""
        state["engine_session_ids"] = engine_sids
        self._state_file.write_text(json.dumps(state))


class FakeCpuWorker:
    instances: list["FakeCpuWorker"] = []

    def __init__(self, session, *, instance_label: str):
        self.session = session
        self.instance_label = instance_label
        self._turn_active = False
        self.started = False
        self.stopped = False
        FakeCpuWorker.instances.append(self)

    async def start(self):
        self.started = True

    async def stop(self):
        self.stopped = True


def test_switch_cpu_rebuilds_worker_and_persists_choice(monkeypatch, tmp_path):
    async def run():
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"session_id": "claude-old"}))
        monkeypatch.setattr(bot, "SESSION_FILE", state_file)
        monkeypatch.setattr(bot, "_STATE_PATH", state_file)
        bot._state = {}
        bot._in_flight = 0
        monkeypatch.setattr(bot, "cc", FakeCpuSession("claude", state_file, "claude-old"))
        old_worker = FakeCpuWorker(bot.cc, instance_label="test")
        monkeypatch.setattr(bot, "_channel_worker", old_worker)
        FakeCpuWorker.instances = [old_worker]

        def fake_make(target=None):
            return FakeCpuSession(target or "claude")

        monkeypatch.setattr(bot, "_make_tg_engine", fake_make)
        monkeypatch.setattr(bot, "ChannelWorker", FakeCpuWorker)

        result = await bot._switch_cpu("codex")

        assert result == "CPU: Claude Code → Codex"
        assert old_worker.stopped is True
        assert bot._channel_worker is FakeCpuWorker.instances[-1]
        assert bot._channel_worker.started is True
        assert bot._current_cpu_name() == "codex"
        state = json.loads(state_file.read_text())
        assert state["assistant_engine"] == "codex"
        assert state["engine_session_ids"]["claude"] == "claude-old"

    asyncio.run(run())


def test_bot_channel_does_not_reach_into_engine_private_session_state():
    source = Path(bot.__file__).read_text()

    assert "cc._session_id" not in source
    assert "cc._record_sid" not in source
    assert "cc._load_state" not in source
    assert 'getattr(cc, "_babata_engine_name"' not in source
    assert 'getattr(cc, "_session_id"' not in source


def test_bot_commands_are_filtered_by_cpu():
    claude = [name for name, _ in bot._bot_commands_for_cpu("claude")]
    codex = [name for name, _ in bot._bot_commands_for_cpu("codex")]

    assert "context" in claude
    assert "stop" in claude
    assert "provider" in claude
    assert "context" not in codex
    assert "stop" in codex
    assert "provider" in codex
    assert {"new", "resume", "status", "verbose", "cpu", "stop", "restart", "provider"} <= set(codex)


def test_codex_rejects_context_supports_stop_and_shows_provider(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "ALLOWED_USER", 7)
        monkeypatch.setattr(bot, "cc", FakeCpuSession("codex"))
        monkeypatch.setattr(bot, "_current_codex_key", lambda: "personal")
        monkeypatch.setattr(bot, "_current_codex_label", lambda: "Codex · personal")
        monkeypatch.setattr(bot, "_codex_choices", lambda: [("Codex · personal", "personal")])
        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()
        monkeypatch.setattr(bot, "_channel_worker", worker)

        ctx = FakeCtx()
        chat = FakeChat()

        context_msg = FakeMessage(10, "/context")
        await bot.cmd_context(FakeUpdate(context_msg, chat, user_id=7), ctx)
        assert "不支持" in context_msg.replies[-1].text

        active_msg = FakeMessage(9, "active")
        await worker.submit(
            bot.Payload(update=FakeUpdate(active_msg, chat), ctx=ctx, text="active")
        )
        stop_msg = FakeMessage(11, "/stop")
        await bot.cmd_stop(FakeUpdate(stop_msg, chat, user_id=7), ctx)
        assert stop_msg.replies == []
        assert session.interrupted is True

        provider_msg = FakeMessage(12, "/provider")
        await bot.cmd_provider(FakeUpdate(provider_msg, chat, user_id=7), ctx)
        assert "Codex 账号" in provider_msg.replies[-1].text
        await worker.stop()

    asyncio.run(run())


def test_codex_rejects_provider_callback(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "ALLOWED_USER", 7)
        monkeypatch.setattr(bot, "cc", FakeCpuSession("codex"))

        async def fail_switch(*args, **kwargs):
            raise AssertionError("provider switch should not run in Codex mode")

        monkeypatch.setattr(bot, "_run_cc_router_switch", fail_switch)
        query = FakeCallbackQuery(user_id=7, data="provider:openrouter")

        await bot.on_provider_click(FakeCallbackUpdate(query), FakeCtx())

        assert query.answers == [(None, {})]
        assert "失效" in query.edits[-1][0]

    asyncio.run(run())


def test_codex_resume_picker_only_shows_current_channel(monkeypatch, tmp_path):
    reset_bot_globals(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "cc", FakeCpuSession("codex", sid="sid-12345678"))

    _header, markup = bot._render_resume_channel_picker()

    buttons = markup[0][0]
    assert len(buttons) == 1
    button = buttons[0][0]
    assert button[0][0] == "当前 Codex"
    assert button[1]["callback_data"] == "resume-ch:tg"


def test_codex_status_reads_session_usage(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "ALLOWED_USER", 7)
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"recent_sids": ["sid-1"]}))
        monkeypatch.setattr(bot, "cc", FakeCpuSession("codex", state_file, "sid-1"))
        monkeypatch.setattr(bot, "_last_model", "codex")
        monkeypatch.setattr(bot, "_codex_version", lambda: "0.128.0")
        session_file = tmp_path / "rollout-sid-1.jsonl"
        session_file.write_text("\n".join([
            json.dumps({
                "type": "turn_context",
                "payload": {
                    "model": "gpt-5.5",
                    "effort": "xhigh",
                },
            }),
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": 1000,
                            "output_tokens": 200,
                            "reasoning_output_tokens": 50,
                        },
                        "model_context_window": 2000,
                    },
                },
                "rate_limits": {
                    "primary": {"used_percent": 12, "resets_at": 1_778_418_860},
                    "secondary": {"used_percent": 34, "resets_at": 1_778_911_266},
                    "plan_type": "prolite",
                },
            }),
        ]))
        monkeypatch.setattr(bot, "_codex_session_file", lambda sid: session_file)
        monkeypatch.setattr(bot, "_codex_sessions_root", lambda: tmp_path)
        monkeypatch.setattr(bot, "_codex_config", lambda: {"model": "gpt-5.5", "model_reasoning_effort": "xhigh"})
        monkeypatch.setattr(bot, "_fetch_codex_app_rate_limits", lambda: asyncio.sleep(0, result=None))

        msg = FakeMessage(13, "/status")
        await bot.cmd_status(FakeUpdate(msg, FakeChat(), user_id=7), FakeCtx())

        text = msg.replies[-1].text
        assert "50%" in text
        assert "gpt-5.5 xhigh" in text
        assert "1.0K in" in text
        assert "200 out" in text
        assert "50 reasoning" in text
        assert "5h limit 88% left" in text
        assert "weekly limit 66% left" in text
        assert "plan prolite" in text
        assert "Codex v0.128.0" in text
        assert "current <code>gpt-5.5</code> · effort <code>xhigh</code>" not in text

    asyncio.run(run())


def test_codex_status_prefers_live_app_server_limits(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "ALLOWED_USER", 7)
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"recent_sids": ["sid-1"]}))
        monkeypatch.setattr(bot, "cc", FakeCpuSession("codex", state_file, "sid-1"))
        monkeypatch.setattr(bot, "_last_model", "codex")
        monkeypatch.setattr(bot, "_codex_version", lambda: "0.128.0")
        session_file = tmp_path / "rollout-sid-1.jsonl"
        session_file.write_text("\n".join([
            json.dumps({
                "type": "turn_context",
                "payload": {"model": "gpt-5.5", "effort": "xhigh"},
            }),
            json.dumps({
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {"input_tokens": 1000},
                        "model_context_window": 2000,
                    },
                },
                "rate_limits": {
                    "primary": {"used_percent": 12, "resets_at": 1_778_418_860},
                    "secondary": {"used_percent": 34, "resets_at": 1_778_911_266},
                    "plan_type": "stale",
                },
            }),
        ]))
        monkeypatch.setattr(bot, "_codex_session_file", lambda sid: session_file)
        monkeypatch.setattr(bot, "_codex_config", lambda: {"model": "gpt-5.5", "model_reasoning_effort": "xhigh"})
        monkeypatch.setattr(bot, "_fetch_codex_app_rate_limits", lambda: asyncio.sleep(0, result={
            "primary": {"used_percent": 29, "window_minutes": 300, "resets_at": 1_778_494_266},
            "secondary": {"used_percent": 33, "window_minutes": 10_080, "resets_at": 1_778_911_266},
            "plan_type": "prolite",
        }))

        msg = FakeMessage(13, "/status")
        await bot.cmd_status(FakeUpdate(msg, FakeChat(), user_id=7), FakeCtx())

        text = msg.replies[-1].text
        assert "5h limit 71% left" in text
        assert "weekly limit 67% left" in text
        assert "plan prolite" in text
        assert "5h limit 88% left" not in text
        assert "plan stale" not in text

    asyncio.run(run())


def test_codex_status_snapshot_reads_collaboration_settings(monkeypatch, tmp_path):
    session_file = tmp_path / "rollout-sid-2.jsonl"
    session_file.write_text("\n".join([
        "not json",
        json.dumps({
            "type": "turn_context",
            "payload": {
                "model": "base-model",
                "effort": "low",
                "collaboration_mode": {
                    "settings": {
                        "model": "gpt-5.5",
                        "reasoning_effort": "xhigh",
                    },
                },
            },
        }),
        json.dumps({
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 1234},
                    "model_context_window": 4096,
                },
            },
            "rate_limits": {
                "primary": {"used_percent": 10, "window_minutes": 300},
            },
        }),
    ]))
    monkeypatch.setattr(bot, "_codex_session_file", lambda sid: session_file)
    monkeypatch.setattr(
        bot,
        "_codex_config",
        lambda: {"model": "configured-model", "model_reasoning_effort": "medium"},
    )

    snap = bot._codex_status_snapshot("sid-2")

    assert snap["model"] == "gpt-5.5"
    assert snap["effort"] == "xhigh"
    assert snap["configured_model"] == "configured-model"
    assert snap["configured_effort"] == "medium"
    assert snap["context_window"] == 4096
    assert snap["context_used"] == 1234
    assert snap["rate_limits"]["primary"]["used_percent"] == 10


def test_claude_status_renders_provider_usage_snapshot(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        monkeypatch.setattr(bot, "ALLOWED_USER", 7)
        state_file = tmp_path / "state.json"
        state_file.write_text(json.dumps({"recent_sids": ["sid-claude"]}))
        monkeypatch.setattr(bot, "cc", FakeCpuSession("claude", state_file, "sid-claude"))
        monkeypatch.setattr(bot, "_last_model", "claude-opus-4-7[1m]")
        monkeypatch.setattr(bot, "_last_context_window", 1_000_000)
        monkeypatch.setattr(bot, "_last_used_tokens", 0)
        monkeypatch.setattr(bot, "_last_session_id", "sid-claude")
        monkeypatch.setattr(bot, "_last_prompt_tokens", lambda _sid: 250_000)
        monkeypatch.setattr(bot, "_fmt_review_health_line", lambda: "review ok · soft")
        monkeypatch.setattr(bot, "_cc_version", lambda: "2.1.112")
        monkeypatch.setattr(bot, "_sdk_version", lambda: "0.2.4")
        monkeypatch.setattr(bot, "_fetch_anthropic_quota", lambda _token: asyncio.sleep(0, result={
            "five_hour": {"utilization": 0.25, "resets_at": 1_778_418_860},
            "seven_day": {"utilization": 0.50, "resets_at": 1_778_911_266},
        }))
        monkeypatch.setattr(bot, "_fetch_or_usage", lambda _key: asyncio.sleep(0, result={
            "data": {"usage_daily": 1.25, "limit_remaining": 18.75},
        }))
        monkeypatch.setattr(bot, "_fetch_ccusage_today", lambda: asyncio.sleep(0, result=3.5))
        monkeypatch.setattr(bot, "_load_providers", lambda: {
            "current": "claude-main",
            "providers": {
                "claude-main": {
                    "display_name": "Claude Main",
                    "env": {"CLAUDE_CODE_OAUTH_TOKEN": "token"},
                },
                "openrouter": {
                    "display_name": "OpenRouter",
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://openrouter.ai/api/v1",
                        "ANTHROPIC_AUTH_TOKEN": "or-token",
                    },
                },
            },
        })

        msg = FakeMessage(13, "/status")
        await bot.cmd_status(FakeUpdate(msg, FakeChat(), user_id=7), FakeCtx())

        text = msg.replies[-1].text
        assert "25%" in text
        assert "Opus 4.7 (1M)" in text
        assert "review ok · soft" in text
        assert "session 25%" in text
        assert "week 50%" in text
        assert "openrouter · $1.25 today · $18.75 left" in text
        assert "$3.50 today (ccusage) · Claude Main" in text
        assert "CC v2.1.112 · SDK v0.2.4" in text
        assert "<code>claude-opus-4-7[1m]</code>" in text
        assert "<code>sid-claude</code> · 1 recent" in text

    asyncio.run(run())


def test_claude_status_context_ignores_stale_last_tokens(monkeypatch, tmp_path):
    reset_bot_globals(monkeypatch, tmp_path)
    monkeypatch.setattr(bot, "_last_model", "claude-opus-4-7[1m]")
    monkeypatch.setattr(bot, "_last_context_window", 1_000)
    monkeypatch.setattr(bot, "_last_used_tokens", 900)
    monkeypatch.setattr(bot, "_last_session_id", "old-sid")
    monkeypatch.setattr(bot, "_last_prompt_tokens", lambda _sid: 0)

    snap = bot._claude_status_context_snapshot("new-sid")

    assert snap["pct_ctx"] == 0.0
    assert snap["bar"] == "░░░░░░░░░░░░░░░"


def test_codex_rate_limits_normalizes_app_server_shape():
    result = {
        "rateLimits": {
            "limitId": "codex",
            "primary": {"usedPercent": 40, "windowDurationMins": 300, "resetsAt": 111},
            "secondary": {"usedPercent": 50, "windowDurationMins": 10_080, "resetsAt": 222},
            "planType": "prolite",
        },
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "limitName": None,
                "primary": {"usedPercent": 29, "windowDurationMins": 300, "resetsAt": 333},
                "secondary": {"usedPercent": 33, "windowDurationMins": 10_080, "resetsAt": 444},
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
                "planType": "prolite",
                "rateLimitReachedType": None,
            },
        },
    }

    normalized = bot._normalize_codex_rate_limits_response(result)

    assert normalized == {
        "limit_id": "codex",
        "limit_name": None,
        "primary": {"used_percent": 29, "window_minutes": 300, "resets_at": 333},
        "secondary": {"used_percent": 33, "window_minutes": 10_080, "resets_at": 444},
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "plan_type": "prolite",
        "rate_limit_reached_type": None,
    }


def test_codex_rate_limits_rejects_empty_app_server_snapshot():
    result = {
        "rateLimitsByLimitId": {
            "codex": {
                "limitId": "codex",
                "planType": "prolite",
            },
        },
    }

    assert bot._normalize_codex_rate_limits_response(result) is None


def test_stream_display_text_keeps_short_text_and_truncates_tail():
    short = "hello"
    long_text = "x" * (bot._MAX_TG + 10)

    assert bot._stream_display_text(short) == short
    display = bot._stream_display_text(long_text)
    assert len(display) == bot._MAX_TG
    assert display.startswith("…")
    assert display.endswith("x" * (bot._MAX_TG - 1))


def test_pending_response_bubbles_drops_empty_edges_and_skips_streamed():
    pending, has_bubbles = bot._pending_response_bubbles(
        "\n\n\none\n\n\ntwo\n\n\n",
        streamed_count=1,
    )

    assert pending == ["two"]
    assert has_bubbles is True


def test_pending_response_bubbles_reports_streamed_empty_final_as_done():
    pending, has_bubbles = bot._pending_response_bubbles(
        "\n\n\n",
        streamed_count=1,
    )

    assert pending == []
    assert has_bubbles is True


def test_channel_worker_reply_anchor_prefers_active_payload(monkeypatch, tmp_path):
    reset_bot_globals(monkeypatch, tmp_path)
    worker = bot.ChannelWorker(FakeSession(), instance_label="test")
    chat = FakeChat()
    latest = bot.Payload(update=FakeUpdate(FakeMessage(1), chat), ctx=FakeCtx(), text="latest")
    turn = bot.Payload(update=FakeUpdate(FakeMessage(2), chat), ctx=FakeCtx(), text="turn")
    active = bot.Payload(update=FakeUpdate(FakeMessage(3), chat), ctx=FakeCtx(), text="active")

    worker._latest_payload = latest
    worker._turn_payload = turn
    worker._active_reply_payload = active
    worker._anchor_generation = 7

    anchor = worker._current_reply_anchor()

    assert anchor is not None
    assert anchor.generation == 7
    assert anchor.payload is active
    assert anchor.message.message_id == 3
    assert anchor.chat is chat


def test_channel_worker_send_completed_text_bubble_formats_and_tracks(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        worker = bot.ChannelWorker(FakeSession(), instance_label="test")
        message = FakeMessage(1, "hello")

        sent = await worker._send_completed_text_bubble(
            message,
            "**done**",
            worker._anchor_generation,
        )

        assert sent == bot.TextBubbleSendResult(sent_ok=True, shipped_msgs=message.replies)
        assert len(message.replies) == 1
        assert message.replies[0].text == "<b>done</b>"
        assert message.replies[0].parse_mode == "HTML"

    asyncio.run(run())


def test_channel_worker_completed_text_bubble_plain_fallback_on_edit_reject(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        worker = bot.ChannelWorker(FakeSession(), instance_label="test")
        message = FakeMessage(1, "hello")
        existing = HtmlRejectingSentMessage("partial")
        worker._text_message = existing

        sent = await worker._send_completed_text_bubble(
            message,
            "**done**",
            worker._anchor_generation,
        )

        assert sent == bot.TextBubbleSendResult(sent_ok=True, shipped_msgs=[existing])
        assert existing.text == "**done**"
        assert existing.edits[-1] == ("**done**", None, None)
        assert worker._stale_text_messages == []

    asyncio.run(run())


def test_channel_worker_completed_text_bubble_plain_fallback_on_reply_reject(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        worker = bot.ChannelWorker(FakeSession(), instance_label="test")
        message = HtmlRejectingMessage(1, "hello")

        sent = await worker._send_completed_text_bubble(
            message,
            "**done**",
            worker._anchor_generation,
        )

        assert sent == bot.TextBubbleSendResult(sent_ok=True, shipped_msgs=message.replies)
        assert len(message.replies) == 1
        assert message.replies[0].text == "**done**"
        assert message.replies[0].parse_mode is None

    asyncio.run(run())


def test_channel_worker_text_delta_closes_multiple_stream_bubbles(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        worker = bot.ChannelWorker(FakeSession(), instance_label="test")
        chat = FakeChat()
        message = FakeMessage(1, "hello")
        payload = bot.Payload(update=FakeUpdate(message, chat), ctx=FakeCtx(), text="hello")
        worker._active_reply_payload = payload
        worker._anchor_generation = 1

        await worker._handle_text_delta("one\n\n\ntwo\n\n\nthree")

        assert [reply.text for reply in message.replies] == ["one", "two", "three"]
        assert worker._streamed_bubble_count == 2
        assert worker._text_buffer == "three"
        assert worker._text_message is message.replies[-1]

    asyncio.run(run())


def test_channel_worker_response_bubble_plain_fallback_on_reply_reject(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        worker = bot.ChannelWorker(FakeSession(), instance_label="test")
        message = HtmlRejectingMessage(1, "hello")

        sent = await worker._send_response_bubble(message, "**done**")

        assert sent is True
        assert len(message.replies) == 1
        assert message.replies[0].text == "**done**"
        assert message.replies[0].parse_mode is None

    asyncio.run(run())


def test_channel_worker_finalize_stream_text_plain_fallback_on_edit_reject(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        worker = bot.ChannelWorker(FakeSession(), instance_label="test")
        message = FakeMessage(1, "hello")
        existing = HtmlRejectingSentMessage("partial")
        worker._text_message = existing

        sent = await worker._finalize_stream_text_message(message, "**done**")

        assert sent is True
        assert existing.text == "**done**"
        assert existing.edits[-1] == ("**done**", None, None)
        assert message.replies == []
        assert worker._stale_text_messages == []

    asyncio.run(run())


def test_channel_worker_finalize_stream_text_resends_when_edit_fails(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        worker = bot.ChannelWorker(FakeSession(), instance_label="test")
        message = FakeMessage(1, "hello")
        existing = RejectingSentMessage("partial")
        worker._text_message = existing

        sent = await worker._finalize_stream_text_message(message, "**done**")

        assert sent is True
        assert worker._stale_text_messages == [existing]
        assert len(message.replies) == 1
        assert message.replies[0].text == "<b>done</b>"
        assert message.replies[0].parse_mode == "HTML"

    asyncio.run(run())


def test_channel_worker_deliver_turn_response_acks_and_accounts(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        recorded = []
        acked = []

        async def fake_record_pending_delivery(payload, resp, update_ids):
            recorded.append((payload.text, resp.content, list(update_ids)))
            return "delivery-1"

        async def fake_ack_pending_delivery(delivery_id):
            acked.append(delivery_id)

        monkeypatch.setattr(bot, "_record_pending_delivery", fake_record_pending_delivery)
        monkeypatch.setattr(bot, "_ack_pending_delivery", fake_ack_pending_delivery)
        worker = bot.ChannelWorker(FakeSession(), instance_label="test")
        chat = FakeChat()
        message = FakeMessage(1, "hello")
        payload = bot.Payload(
            update=FakeUpdate(message, chat),
            ctx=FakeCtx(),
            text="hello",
            update_id=101,
        )
        resp = Response(content="done", session_id="sid-1", cost=0.25)

        ok = await worker._deliver_turn_response(payload, resp, [101, None])

        assert ok is True
        assert recorded == [("hello", "done", [101, None])]
        assert acked == ["delivery-1"]
        assert message.replies[0].text == "done"
        assert bot._session_turns == 1
        assert bot._last_cost == 0.25

    asyncio.run(run())


def test_claude_status_lines_escape_and_include_optional_usage():
    lines = bot._claude_status_lines(
        bar="[##]",
        pct_ctx=25.0,
        model_short="Opus <4>",
        window_short="1M",
        review_line="review <ok>",
        session_line="session 25%",
        week_line=None,
        or_today_line="$1.00 today",
        or_balance_line="$9.00 left USD",
        or_compact_line=None,
        today_line="$3.50 today (ccusage) · Claude Main",
        cc_version="2.1.112",
        sdk_version="0.2.4",
        verbose=1,
        actual="claude-opus-4<id>",
        sid_now="sid-1",
        recent_count=2,
    )

    text = "\n".join(lines)
    assert "Opus &lt;4&gt;" in text
    assert "review &lt;ok&gt;" in text
    assert "session 25%" in text
    assert "$1.00 today" in text
    assert "$9.00 left USD" in text
    assert "CC v2.1.112 · SDK v0.2.4 · flash" in text
    assert "<code>claude-opus-4&lt;id&gt;</code>" in text
    assert "<code>sid-1</code> · 2 recent" in text


def test_openrouter_usage_lines_current_and_secondary():
    assert bot._openrouter_usage_lines(
        is_current=True,
        data={"usage": 1.25, "usage_daily": 0.50, "limit_remaining": 8.75},
    ) == ("$0.50 today", "$1.25 used · $8.75 left USD", None)

    assert bot._openrouter_usage_lines(
        is_current=False,
        data={"usage_daily": 1.25, "limit_remaining": 18.75},
    ) == (None, None, "openrouter · $1.25 today · $18.75 left")


def test_channel_worker_single_turn_clean_reset(monkeypatch, tmp_path):
    """Baseline: one user msg → one turn → in_flight returns to 0."""
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()

        chat = FakeChat()
        msg = FakeMessage(1, "hello")
        await worker.submit(
            bot.Payload(update=FakeUpdate(msg, chat), ctx=FakeCtx(), text="hello")
        )
        assert bot._in_flight == 1
        assert worker._turn_anchor == 1

        session.queue.put_nowait(Event(kind="text_delta", chunk="Hi"))
        await wait_for(lambda: len(msg.replies) == 1)

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(
                    content="done", session_id="sid-1", cost=0.1,
                ),
            )
        )
        await wait_for(lambda: bot._in_flight == 0)
        assert worker._turn_active is False
        assert bot._session_turns == 1

        await worker.stop()

    asyncio.run(run())


def test_channel_worker_cut_in_waits_for_next_turn(monkeypatch, tmp_path):
    """V 快速连发: 第二条先 ack + interrupt, 但不立即 submit 进 SDK.
    等第一条 turn_end 后, worker 才 begin_turn + submit 第二条, 避免 stale
    interrupt 命中新 turn."""
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()

        chat = FakeChat()
        first_msg = FakeMessage(1, "hello")
        second_msg = FakeMessage(2, "more")
        ctx = FakeCtx()

        await worker.submit(
            bot.Payload(update=FakeUpdate(first_msg, chat), ctx=ctx, text="hello")
        )
        await worker.submit(
            bot.Payload(update=FakeUpdate(second_msg, chat), ctx=ctx, text="more")
        )

        assert session.submitted == [("hello", None)]
        assert bot._in_flight == 1
        await wait_for(lambda: session.interrupted is True)
        # SDK turn anchor 仍是 msg1; cut-in 不改 bridge reply_to.
        assert worker._turn_anchor == 1
        assert bot.bridge.contexts[-1][2] == 1

        # 第一 turn 的 text/tool 继续落到 msg1, 不污染尚未开始的 msg2.
        session.queue.put_nowait(Event(kind="text_delta", chunk="Hi"))
        await wait_for(lambda: len(first_msg.replies) == 1)
        live_text = first_msg.replies[0]
        assert live_text.text == "Hi"
        assert len(second_msg.replies) == 0

        session.queue.put_nowait(
            Event(kind="tool_use", name="Read", input_dict={"file_path": "a.py"})
        )
        await wait_for(lambda: len(first_msg.replies) == 2)
        tool_status = first_msg.replies[1]
        assert "Read" in tool_status.text

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(
                    content="**done**",
                    session_id="sid-1",
                    cost=0.2,
                    model="claude-test[200k]",
                    context_window=200000,
                    input_tokens=5,
                    cache_creation_tokens=1,
                    cache_read_tokens=2,
                ),
            )
        )
        # 第一 turn_end 后立即启动第二条 queued payload.
        await wait_for(lambda: session.submitted == [("hello", None), ("more", None)])
        assert bot._in_flight == 1
        assert worker._turn_anchor == 2
        assert bot.bridge.contexts[-1][2] == 2
        # final response 编辑第一条的 live text.
        assert live_text.edits[-1][0] == "<b>done</b>"
        assert tool_status.deleted is True
        assert bot._session_turns == 1
        assert bot._last_used_tokens == 8

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(
                    content="second done",
                    session_id="sid-2",
                    cost=0.1,
                ),
            )
        )
        await wait_for(lambda: bot._in_flight == 0)
        assert any("second done" in r.text for r in second_msg.replies)
        assert bot._session_turns == 2
        assert worker._turn_active is False

        await worker.stop()
        assert session.closed

    asyncio.run(run())


def test_channel_worker_codex_coalesces_pending_cut_ins(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        processed: list[int] = []

        async def fake_mark_processed(update_id):
            if update_id is not None:
                processed.append(update_id)

        monkeypatch.setattr(bot, "_mark_processed", fake_mark_processed)
        session = FakeSession()
        session.supports_hot_input = False
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()

        chat = FakeChat(chat_id=42)
        ctx = FakeCtx()
        m1 = FakeMessage(1, "first")
        m2 = FakeMessage(2, "second")
        m3 = FakeMessage(3, "third")

        await worker.submit(
            bot.Payload(
                update=FakeUpdate(m1, chat),
                ctx=ctx,
                text="first",
                update_id=101,
            )
        )
        await worker.submit(
            bot.Payload(
                update=FakeUpdate(m2, chat),
                ctx=ctx,
                text="second",
                update_id=102,
            )
        )
        await worker.submit(
            bot.Payload(
                update=FakeUpdate(m3, chat),
                ctx=ctx,
                text="third",
                update_id=103,
            )
        )

        assert session.submitted == [("first", None)]
        assert session.interrupted is False

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="done1", session_id="sid-1", cost=0.01),
            )
        )
        await wait_for(lambda: len(session.submitted) == 2)
        prompt, images = session.submitted[1]
        assert images is None
        assert "Multiple Telegram messages, oldest to newest" in prompt
        assert "clarify or supersede" in prompt
        assert "previous turn" not in prompt
        assert "<user_message n=1 update_id=102 message_id=2>" in prompt
        assert "second" in prompt
        assert "<user_message n=2 update_id=103 message_id=3>" in prompt
        assert "third" in prompt
        assert processed == [101]

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="batch done", session_id="sid-2", cost=0.01),
            )
        )
        await wait_for(lambda: processed == [101, 102, 103])
        await wait_for(lambda: (42, 2, "👌") in ctx.bot.reactions)
        await wait_for(lambda: (42, 3, "👌") in ctx.bot.reactions)
        assert len(m2.replies) == 0
        assert any("batch done" in r.text for r in m3.replies)
        await wait_for(lambda: bot._in_flight == 0)

        await worker.stop()

    asyncio.run(run())

def test_channel_worker_stopped_turn_marks_active_failed(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()

        chat = FakeChat(chat_id=42)
        ctx = FakeCtx()
        msg = FakeMessage(1, "stop me")

        await worker.submit(
            bot.Payload(update=FakeUpdate(msg, chat), ctx=ctx, text="stop me")
        )
        await wait_for(lambda: (42, 1, "👀") in ctx.bot.reactions)

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(
                    content="当前 Codex turn 已停止。",
                    session_id="sid-1",
                    cost=0.0,
                    stopped=True,
                ),
            )
        )

        await wait_for(lambda: (42, 1, "💔") in ctx.bot.reactions)
        assert (42, 1, "👌") not in ctx.bot.reactions
        assert any("已停止" in r.text for r in msg.replies)
        await wait_for(lambda: bot._in_flight == 0)

        await worker.stop()

    asyncio.run(run())


def test_channel_worker_reaction_eye_then_ok_single_turn(monkeypatch, tmp_path):
    """单条消息: submit → 👀 立即 fire (因为 _begin_turn inline); turn_end → 👌."""
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()

        chat = FakeChat(chat_id=42)
        msg = FakeMessage(1, "hello")
        ctx = FakeCtx()
        await worker.submit(
            bot.Payload(update=FakeUpdate(msg, chat), ctx=ctx, text="hello")
        )
        await wait_for(lambda: (42, 1, "👀") in ctx.bot.reactions)
        # turn_end 之前不该出现 👌
        assert (42, 1, "👌") not in ctx.bot.reactions

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="hi", session_id="sid-1", cost=0.01),
            )
        )
        await wait_for(lambda: (42, 1, "👌") in ctx.bot.reactions)
        assert ctx.bot.reactions == [(42, 1, "👀"), (42, 1, "👌")]

        await worker.stop()

    asyncio.run(run())


def test_channel_worker_reaction_back_to_back_messages(monkeypatch, tmp_path):
    """V 连发两条: 两条都立即 👀; 每条在各自 turn_end 后 👌."""
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()

        chat = FakeChat(chat_id=42)
        m1 = FakeMessage(1, "first")
        m2 = FakeMessage(2, "second")
        ctx = FakeCtx()  # 共享同一个 bot 让 reactions 集中收集

        await worker.submit(
            bot.Payload(update=FakeUpdate(m1, chat), ctx=ctx, text="first")
        )
        await wait_for(lambda: (42, 1, "👀") in ctx.bot.reactions)

        await worker.submit(
            bot.Payload(update=FakeUpdate(m2, chat), ctx=ctx, text="second")
        )
        await wait_for(lambda: (42, 2, "👀") in ctx.bot.reactions)
        assert (42, 2, "👌") not in ctx.bot.reactions

        # 第一 turn_end → 只 finalize m1, 并启动 m2 的 queued turn.
        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="ok1", session_id="sid-1", cost=0.01),
            )
        )
        await wait_for(lambda: (42, 1, "👌") in ctx.bot.reactions)
        assert (42, 2, "👌") not in ctx.bot.reactions
        assert bot._in_flight == 1

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="ok2", session_id="sid-2", cost=0.01),
            )
        )
        await wait_for(lambda: (42, 2, "👌") in ctx.bot.reactions)
        await wait_for(lambda: bot._in_flight == 0)

        await worker.stop()

    asyncio.run(run())


def test_channel_worker_stream_error_replays_active_then_pending(monkeypatch, tmp_path):
    """Recoverable CPU stream error must replay N before continuing N+1.

    The old behavior marked active+pending as processed and 💔, permanently
    dropping the queue. The required channel contract is FIFO replay after the
    supervisor reconnects.
    """
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        processed: list[int] = []

        async def fake_mark_processed(update_id):
            if update_id is not None:
                processed.append(update_id)

        monkeypatch.setattr(bot, "_mark_processed", fake_mark_processed)
        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()

        chat = FakeChat(chat_id=42)
        m1 = FakeMessage(1, "first")
        m2 = FakeMessage(2, "second")
        ctx = FakeCtx()

        await worker.submit(
            bot.Payload(
                update=FakeUpdate(m1, chat),
                ctx=ctx,
                text="first",
                update_id=101,
            )
        )
        await worker.submit(
            bot.Payload(
                update=FakeUpdate(m2, chat),
                ctx=ctx,
                text="second",
                update_id=102,
            )
        )
        assert session.submitted == [("first", None)]

        session.queue.put_nowait(Event(kind="error", exception=RuntimeError("boom")))

        # After reconnect, m1 is replayed first; m2 stays queued.
        await wait_for(lambda: session.submitted == [("first", None), ("first", None)])
        assert processed == []
        assert (42, 1, "💔") not in ctx.bot.reactions
        assert (42, 2, "💔") not in ctx.bot.reactions
        assert bot._in_flight == 1
        assert worker._turn_anchor == 1

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="ok1", session_id="sid-1", cost=0.01),
            )
        )
        await wait_for(
            lambda: session.submitted == [
                ("first", None),
                ("first", None),
                ("second", None),
            ]
        )
        await wait_for(lambda: 101 in processed)
        assert (42, 1, "👌") in ctx.bot.reactions
        assert worker._turn_anchor == 2

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="ok2", session_id="sid-2", cost=0.01),
            )
        )
        await wait_for(lambda: 102 in processed)
        await wait_for(lambda: bot._in_flight == 0)
        assert (42, 2, "👌") in ctx.bot.reactions
        assert (42, 2, "💔") not in ctx.bot.reactions

        await worker.stop()

    asyncio.run(run())


def test_turn_end_delivery_failure_does_not_mark_processed(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        processed: list[int] = []

        async def fake_mark_processed(update_id):
            if update_id is not None:
                processed.append(update_id)

        monkeypatch.setattr(bot, "_mark_processed", fake_mark_processed)

        class FailingMessage(FakeMessage):
            async def reply_text(self, text: str, parse_mode=None, reply_markup=None):
                raise RuntimeError("telegram down")

        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()

        chat = FakeChat(chat_id=42)
        ctx = FakeCtx()
        msg = FailingMessage(7, "needs visible reply")
        await worker.submit(
            bot.Payload(
                update=FakeUpdate(msg, chat),
                ctx=ctx,
                text="needs visible reply",
                update_id=101,
            )
        )

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="done", session_id="sid-1", cost=0.01),
            )
        )

        await wait_for(lambda: bot._in_flight == 0)
        assert processed == []
        assert "101" in bot._pending_delivery_records
        await wait_for(lambda: (42, 7, "💔") in ctx.bot.reactions)

        await worker.stop()

    asyncio.run(run())


def test_pending_delivery_replays_before_model_replay(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)

        ctx = FakeCtx()
        bot._pending_update_records = {
            "501": {
                "update_id": 501,
                "chat_id": 42,
                "message_id": 7,
                "text": "already answered",
                "images": [],
                "received_at": 1.0,
            },
        }
        bot._pending_delivery_records = {
            "501": {
                "delivery_id": "501",
                "update_ids": [501],
                "anchor_update_id": 501,
                "chat_id": 42,
                "message_id": 7,
                "content": "cached final answer",
                "resume_note": None,
                "session_id": "sid-1",
                "created_at": 1.0,
            },
        }

        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        monkeypatch.setattr(bot, "_channel_worker", worker)
        await worker.start()
        app = type("FakeApp", (), {"bot": ctx.bot})()

        delivery_replay = await bot._replay_pending_deliveries(app)
        assert delivery_replay.delivered == 1
        assert ctx.bot.sent_messages[-1]["text"] == "cached final answer"
        assert ctx.bot.sent_messages[-1]["reply_to_message_id"] == 7
        assert 501 in bot._processed_set
        assert bot._pending_delivery_records == {}
        assert bot._pending_update_records == {}

        pending_replay = await bot._replay_pending_updates(app)
        assert pending_replay.replayed == 0
        assert session.submitted == []

        await worker.stop()

    asyncio.run(run())


def test_pending_delivery_replay_skips_processed_and_counts_malformed(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)

        ctx = FakeCtx()
        bot._processed_set = {601}
        bot._pending_delivery_records = {
            "done": {
                "delivery_id": "done",
                "update_ids": [601],
                "anchor_update_id": 601,
                "chat_id": 42,
                "message_id": 7,
                "content": "already delivered",
                "session_id": "sid-1",
                "created_at": 1.0,
            },
            "bad": {
                "delivery_id": "bad",
                "update_ids": [],
                "chat_id": 42,
                "created_at": 2.0,
            },
        }
        app = type("FakeApp", (), {"bot": ctx.bot})()

        summary = await bot._replay_pending_deliveries(app)

        assert summary.skipped_processed == 1
        assert summary.malformed == 1
        assert summary.delivered == 0
        assert "done" not in bot._pending_delivery_records
        assert "bad" in bot._pending_delivery_records

    asyncio.run(run())


def test_tg_pending_update_replays_after_restart_and_acks(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)

        session1 = FakeSession()
        worker1 = bot.ChannelWorker(session1, instance_label="test")
        monkeypatch.setattr(bot, "_channel_worker", worker1)
        await worker1.start()

        ctx = FakeCtx()
        chat = FakeChat(chat_id=42)
        msg = FakeMessage(7, "needs replay")
        update = FakeUpdate(msg, chat, user_id=7, update_id=501)

        await bot._process(update, ctx, "needs replay")
        assert session1.submitted == [("needs replay", None)]
        pending = json.loads((tmp_path / "pending.json").read_text())["pending"]
        assert pending["501"]["text"] == "needs replay"

        # Simulate process death before turn_end: no processed mark was written.
        await worker1.stop()

        session2 = FakeSession()
        worker2 = bot.ChannelWorker(session2, instance_label="test")
        monkeypatch.setattr(bot, "_channel_worker", worker2)
        await worker2.start()
        app = type("FakeApp", (), {"bot": ctx.bot})()

        replay = await bot._replay_pending_updates(app)
        assert replay.total == 1
        assert replay.replayed == 1
        assert replay.failed == 0
        assert session2.submitted == [("needs replay", None)]

        session2.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="done", session_id="sid-1", cost=0.01),
            )
        )
        await wait_for(lambda: "501" not in bot._pending_update_records)
        assert 501 in bot._processed_set
        assert json.loads((tmp_path / "pending.json").read_text())["pending"] == {}
        assert ctx.bot.sent_messages[-1]["reply_to_message_id"] == 7

        await worker2.stop()

    asyncio.run(run())


def test_tg_replay_pending_fifo_without_interrupt(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        ctx = FakeCtx()
        bot._pending_update_records = {
            "501": {
                "update_id": 501,
                "chat_id": 42,
                "message_id": 7,
                "text": "first",
                "images": [],
                "received_at": 1.0,
            },
            "502": {
                "update_id": 502,
                "chat_id": 42,
                "message_id": 8,
                "text": "second",
                "images": [],
                "received_at": 2.0,
            },
        }

        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        monkeypatch.setattr(bot, "_channel_worker", worker)
        await worker.start()

        app = type("FakeApp", (), {"bot": ctx.bot})()
        replay = await bot._replay_pending_updates(app)
        assert replay.total == 2
        assert replay.replayed == 2
        assert replay.failed == 0

        assert session.submitted == [("first", None)]
        assert session.interrupted is False
        assert [p.text for p in worker._pending_payloads] == ["second"]

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="done1", session_id="sid-1", cost=0.01),
            )
        )
        await wait_for(lambda: session.submitted == [("first", None), ("second", None)])

        await worker.stop()

    asyncio.run(run())


def test_tg_startup_notice_surfaces_pending_replay_summary():
    assert bot._pending_replay_notice_lines(
        bot.PendingReplaySummary(total=1, replayed=1)
    ) == ["已恢复 1 个未完成任务"]

    assert bot._pending_replay_notice_lines(
        bot.PendingReplaySummary(total=3, replayed=2, failed=1)
    ) == ["已恢复 2 个未完成任务", "⚠️ 1 个未完成任务恢复失败，见日志"]


def test_tg_shutdown_records_new_update_for_restart_replay(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        bot._shutdown_requested = True

        ctx = FakeCtx()
        chat = FakeChat(chat_id=42)
        msg = FakeMessage(7, "during restart")
        update = FakeUpdate(msg, chat, user_id=7, update_id=501)

        await bot._process(update, ctx, "during restart")

        assert json.loads((tmp_path / "pending.json").read_text())["pending"]["501"]["text"] == "during restart"

    asyncio.run(run())


def test_channel_worker_unfinished_work_includes_pending_cut_ins(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()

        ctx = FakeCtx()
        chat = FakeChat(chat_id=42)
        await worker.submit(
            bot.Payload(
                update=FakeUpdate(FakeMessage(7, "first"), chat, update_id=501),
                ctx=ctx,
                text="first",
                update_id=501,
            )
        )
        await worker.submit(
            bot.Payload(
                update=FakeUpdate(FakeMessage(8, "second"), chat, update_id=502),
                ctx=ctx,
                text="second",
                update_id=502,
            )
        )

        assert await worker.has_unfinished_work() is True
        assert [p.text for p in worker._pending_payloads] == ["second"]

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="done1", session_id="sid-1", cost=0.01),
            )
        )
        await wait_for(lambda: session.submitted == [("first", None), ("second", None)])
        assert await worker.has_unfinished_work() is True

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="done2", session_id="sid-2", cost=0.01),
            )
        )
        await wait_for(lambda: bot._in_flight == 0)
        assert await worker.has_unfinished_work() is False

        await worker.stop()

    asyncio.run(run())


def test_channel_worker_new_message_reply_does_not_merge(monkeypatch, tmp_path):
    """V 发 msg1 流式中又发 msg2: msg1 的后续输出仍在 msg1, msg2
    等第一 turn_end 后开启自己的 reply."""
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()

        chat = FakeChat(chat_id=42)
        m1 = FakeMessage(1, "first")
        m2 = FakeMessage(2, "second")
        ctx = FakeCtx()

        await worker.submit(
            bot.Payload(update=FakeUpdate(m1, chat), ctx=ctx, text="first")
        )
        # 第一条流式输出: 应 reply 到 m1
        session.queue.put_nowait(Event(kind="text_delta", chunk="answer-1-part-A"))
        await wait_for(lambda: len(m1.replies) == 1)
        first_reply = m1.replies[0]
        assert "answer-1-part-A" in first_reply.text

        # V 中途发 msg2 (turn 1 还没 turn_end)
        await worker.submit(
            bot.Payload(update=FakeUpdate(m2, chat), ctx=ctx, text="second")
        )

        # 后续 text_delta 仍属于第一 turn. 因 edit throttle 不保证立即刷出,
        # 但绝不能落到尚未 begin_turn 的 msg2.
        session.queue.put_nowait(Event(kind="text_delta", chunk="answer-2"))
        await asyncio.sleep(0.05)
        assert len(m2.replies) == 0
        assert len(m1.replies) == 1

        # 第一 turn 结束后, queued msg2 才 begin_turn; 新流式输出落到 msg2.
        session.queue.put_nowait(
            Event(kind="turn_end", response=Response(content="", session_id="sid-1", cost=0.01))
        )
        await wait_for(lambda: session.submitted == [("first", None), ("second", None)])
        session.queue.put_nowait(Event(kind="text_delta", chunk="answer-msg2"))
        await wait_for(lambda: len(m2.replies) == 1)
        assert m2.replies[0].text == "answer-msg2"

        # 关掉 worker (turn_end 不发, 让 stop 自己 drain)
        await worker.stop()

    asyncio.run(run())


def test_channel_worker_final_response_lands_on_active_reply_anchor(monkeypatch, tmp_path):
    """Cut-in 模式: msg1 final 留在 msg1 anchor; msg2 final 等自己的 turn_end."""
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()

        chat = FakeChat(chat_id=42)
        m1 = FakeMessage(1, "first")
        m2 = FakeMessage(2, "second")
        ctx = FakeCtx()

        await worker.submit(
            bot.Payload(update=FakeUpdate(m1, chat), ctx=ctx, text="first")
        )
        # 第一条流式输出: reply 到 m1
        session.queue.put_nowait(Event(kind="text_delta", chunk="streaming-msg1"))
        await wait_for(lambda: len(m1.replies) == 1)

        # V 中途发 msg2 — 只 queue + interrupt, 不切当前 turn anchor.
        await worker.submit(
            bot.Payload(update=FakeUpdate(m2, chat), ctx=ctx, text="second")
        )

        # SDK turn_end 来 — final response 应落 msg1, 然后启动 msg2.
        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(
                    content="final answer", session_id="sid-1", cost=0.05,
                ),
            )
        )
        await wait_for(lambda: "final answer" in m1.replies[0].text)
        assert len(m2.replies) == 0
        await wait_for(lambda: session.submitted == [("first", None), ("second", None)])

        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(
                    content="second final", session_id="sid-2", cost=0.05,
                ),
            )
        )
        await wait_for(lambda: any("second final" in r.text for r in m2.replies))

        await worker.stop()

    asyncio.run(run())


def test_channel_worker_reset_drops_pending_marks(monkeypatch, tmp_path):
    """P2-D: V 发 m1 → submit (pending=[m1]) → /new → reset_turn_state 应清
    pending_marks. 接着 V 发 m2 → 只有 m2 进 pending, 不会带着 m1 一起 fire 👀."""
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()

        chat = FakeChat(chat_id=42)
        ctx = FakeCtx()

        m1 = FakeMessage(1, "first")
        await worker.submit(
            bot.Payload(update=FakeUpdate(m1, chat), ctx=ctx, text="first")
        )
        await wait_for(lambda: (42, 1, "👀") in ctx.bot.reactions)

        # V /new — 走 _handle_reset 路径
        new_msg = FakeMessage(99, "/new")
        await worker.submit(
            bot.Payload(update=FakeUpdate(new_msg, chat), ctx=ctx, text="/new")
        )
        # /new 后 _pending_marks 应被清空 (drop_pending=True 路径)
        assert worker._pending_marks == []
        assert worker._active_marks == []
        # m1 在 /new 时被 fire 💔 (区分 turn_end 的 👌, V 一眼看到未完成).
        await wait_for(lambda: (42, 1, "💔") in ctx.bot.reactions)

        # V 接着发 m2 — 只有 m2 进 pending → 👀 给 m2
        m2 = FakeMessage(2, "after-reset")
        await worker.submit(
            bot.Payload(update=FakeUpdate(m2, chat), ctx=ctx, text="after-reset")
        )
        await wait_for(lambda: (42, 2, "👀") in ctx.bot.reactions)

        # 关键断言: m1 的 (chat=42, msg=1) 不应该再次 fire 👀 (它在 reset 里被丢了)
        # 第一次 m1 submit 时已 fire 过一次 👀, 但 /new 之后不应再有第二次
        eye_for_m1 = [r for r in ctx.bot.reactions if r == (42, 1, "👀")]
        assert len(eye_for_m1) == 1, (
            f"m1 should have fired 👀 exactly once, got {eye_for_m1}, "
            f"all reactions: {ctx.bot.reactions}"
        )

        await worker.stop()

    asyncio.run(run())


def test_channel_worker_reset_shortcut(monkeypatch, tmp_path):
    async def run():
        reset_bot_globals(monkeypatch, tmp_path)
        session = FakeSession()
        worker = bot.ChannelWorker(session, instance_label="test")
        await worker.start()

        msg = FakeMessage(1, "/new")
        await worker.submit(
            bot.Payload(update=FakeUpdate(msg, FakeChat()), ctx=FakeCtx(), text="/new")
        )
        assert session.reset is True
        assert msg.replies[0].text == "会话已重置。"
        assert bot._in_flight == 0

        await worker.stop()

    asyncio.run(run())
