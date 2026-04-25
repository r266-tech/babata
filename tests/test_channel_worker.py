import asyncio
import os
import sys
from pathlib import Path

SDK_SITE = Path("/Users/admin/code/babata/.venv/lib/python3.13/site-packages")
if SDK_SITE.exists():
    sys.path.insert(0, str(SDK_SITE))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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


class FakeChat:
    def __init__(self, chat_id: int = 42):
        self.id = chat_id
        self.actions = []

    async def send_action(self, action: str):
        self.actions.append(action)


class FakeUpdate:
    def __init__(self, message: FakeMessage, chat: FakeChat):
        self.effective_message = message
        self.message = message
        self.effective_chat = chat
        self.effective_user = None


class FakeCtx:
    def __init__(self):
        self.bot = object()
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
    bot._state = {}
    bot._verbose = 1
    bot._in_flight = 0
    bot._session_cost = 0.0
    bot._session_turns = 0
    bot._last_model = None
    bot._last_context_window = None
    bot._last_used_tokens = 0
    bot._last_cost = 0.0


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


def test_channel_worker_back_to_back_submits_promote_next_anchor(monkeypatch, tmp_path):
    """P1.4: when V sends two messages back-to-back, the second becomes the
    anchor for the next SDK turn instead of being silently dropped."""
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

        assert session.submitted == [("hello", None), ("more", None)]
        assert bot._in_flight == 1
        assert worker._turn_anchor == 1
        assert bot.bridge.contexts[-1][2] == 2

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
        # P1.4 invariant: turn_end finalizes msg1's turn, then the second
        # submit's payload becomes the anchor for the next SDK turn so its
        # text deltas have somewhere to render.
        await wait_for(lambda: worker._turn_anchor == 2)
        assert bot._in_flight == 1  # next turn pre-armed
        assert live_text.edits[-1][0] == "<b>done</b>"
        assert tool_status.deleted is True
        assert bot._session_turns == 1
        assert bot._last_used_tokens == 8

        # Second SDK turn: text delta should hit second_msg's reply, not first
        session.queue.put_nowait(Event(kind="text_delta", chunk="ok"))
        await wait_for(lambda: len(second_msg.replies) == 1)
        assert second_msg.replies[0].text == "ok"

        # Finalize the second turn cleanly
        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="ok2", session_id="sid-2", cost=0.05),
            )
        )
        await wait_for(lambda: bot._in_flight == 0)
        assert bot._session_turns == 2

        await worker.stop()
        assert session.closed

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
