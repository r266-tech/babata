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


class FakeBot:
    """PTB Bot stub. Records set_message_reaction calls so tests can assert
    👀 fired at turn-begin and 👌 at turn-end."""

    def __init__(self):
        self.reactions: list[tuple[int, int, str]] = []

    async def set_message_reaction(self, *, chat_id, message_id, reaction):
        self.reactions.append((chat_id, message_id, reaction))


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
    """P1.4 + per-message reply anchor:
    - SDK turn anchor (P1.4): turn_end 后 promote latest_payload 给下个 SDK turn.
    - V-visible reply anchor: 每条 V 消息进来立刻把流式输出切到自己的 reply.
      所以 first_msg 的 SDK turn 期间, 如果第二条 V msg 已经 submit, 后续的
      text_delta 会 reply 到 second_msg (V 视角"活跃消息"), 不是 first_msg.
    """
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
        # SDK turn anchor 仍是 msg1 (P1.4: 等 turn_end 才 promote)
        assert worker._turn_anchor == 1
        assert bot.bridge.contexts[-1][2] == 2

        # text_delta — per-message reply: 切到 second_msg (最近 submit 的 V 消息)
        session.queue.put_nowait(Event(kind="text_delta", chunk="Hi"))
        await wait_for(lambda: len(second_msg.replies) == 1)
        live_text = second_msg.replies[0]
        assert live_text.text == "Hi"
        assert len(first_msg.replies) == 0  # first_msg 没收到 reply

        session.queue.put_nowait(
            Event(kind="tool_use", name="Read", input_dict={"file_path": "a.py"})
        )
        await wait_for(lambda: len(second_msg.replies) == 2)
        tool_status = second_msg.replies[1]
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
        # P1.4: turn_end 后 promote, _turn_anchor 切到 msg2
        await wait_for(lambda: worker._turn_anchor == 2)
        assert bot._in_flight == 1  # next turn pre-armed
        # final response 编辑当前 _text_message (= second_msg.replies[0])
        assert live_text.edits[-1][0] == "<b>done</b>"
        assert tool_status.deleted is True
        assert bot._session_turns == 1
        assert bot._last_used_tokens == 8

        # 第二个 SDK turn: _begin_turn 重置 _text_message=None, 又新开 reply 到 msg2
        session.queue.put_nowait(Event(kind="text_delta", chunk="ok"))
        await wait_for(lambda: len(second_msg.replies) == 3)
        assert second_msg.replies[2].text == "ok"

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
    """V 连发两条: 第一条 👀 立即, 第二条等. turn_end 后第一条 👌 + 第二条 👀
    自动接管 (P1.4 promote 路径). 下个 turn_end → 第二条 👌."""
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
        # 第二条还没被 picked_up — 当前 turn 还在跑第一条
        await asyncio.sleep(0.05)
        assert (42, 2, "👀") not in ctx.bot.reactions
        assert (42, 2, "👌") not in ctx.bot.reactions

        # turn 1 结束 → 第一条 👌, P1.4 promote 第二条 → 👀
        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="ok1", session_id="sid-1", cost=0.01),
            )
        )
        await wait_for(lambda: (42, 1, "👌") in ctx.bot.reactions)
        await wait_for(lambda: (42, 2, "👀") in ctx.bot.reactions)
        assert (42, 2, "👌") not in ctx.bot.reactions

        # turn 2 结束 → 第二条 👌
        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(content="ok2", session_id="sid-2", cost=0.01),
            )
        )
        await wait_for(lambda: (42, 2, "👌") in ctx.bot.reactions)

        # 顺序: 1👀 → 2 等待 → 1👌 → 2👀 → 2👌
        # (👀 调度 vs 👌 调度顺序在 turn_end finally 里是先 👌 后 _begin_turn 触发 👀)
        assert ctx.bot.reactions[0] == (42, 1, "👀")
        assert ctx.bot.reactions[-1] == (42, 2, "👌")
        assert (42, 1, "👌") in ctx.bot.reactions
        assert (42, 2, "👀") in ctx.bot.reactions

        await worker.stop()

    asyncio.run(run())


def test_channel_worker_new_message_reply_does_not_merge(monkeypatch, tmp_path):
    """V 发 msg1 流式中, 又发 msg2 — msg2 进来后流式输出应该 reply 到 msg2,
    不接着编辑 msg1 的旧 reply (per-message reply anchor)."""
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

        # 后续 text_delta: 应该 reply 到 m2 (开新消息), 不再编辑 m1.replies[0]
        session.queue.put_nowait(Event(kind="text_delta", chunk="answer-2"))
        await wait_for(lambda: len(m2.replies) == 1)
        assert m2.replies[0].text == "answer-2"
        # m1 的旧 reply 停在最后流式状态, 没被 "answer-2" 污染
        assert "answer-2" not in first_reply.text
        # m1 的旧 reply 也没多新消息进来 (只在它 reply 链有第一条)
        assert len(m1.replies) == 1

        # 关掉 worker (turn_end 不发, 让 stop 自己 drain)
        await worker.stop()

    asyncio.run(run())


def test_channel_worker_final_response_lands_on_active_reply_anchor(monkeypatch, tmp_path):
    """P1-C/D: V 发 msg1 流式中 → 发 msg2 → SDK turn_end 来时, final response
    应该落到 msg2 reply (V 视角 active anchor), 不是 msg1.
    long-response overflow parts 也必须同 anchor, 不跨 msg1/msg2 分裂."""
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

        # V 中途发 msg2 — submit 切 active_reply_payload + 清 _text_message
        await worker.submit(
            bot.Payload(update=FakeUpdate(m2, chat), ctx=ctx, text="second")
        )

        # SDK turn_end 来 — final response 应落 msg2 (active anchor), 不落 m1
        session.queue.put_nowait(
            Event(
                kind="turn_end",
                response=Response(
                    content="final answer", session_id="sid-1", cost=0.05,
                ),
            )
        )
        # 等 m2 收到 final response (开新 reply, 因为 _text_message 被 submit 清了)
        await wait_for(lambda: len(m2.replies) >= 1)
        # m2 的 reply 包含 final content
        assert any("final answer" in r.text for r in m2.replies)
        # m1 的旧 reply 没被 final response 污染 — 停在最后流式状态
        assert "final answer" not in m1.replies[0].text
        assert "streaming-msg1" in m1.replies[0].text

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
