import asyncio
import json
import sys
from pathlib import Path

SDK_SITE = Path("/Users/admin/code/babata/.venv/lib/python3.13/site-packages")
if SDK_SITE.exists():
    sys.path.insert(0, str(SDK_SITE))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import cc
from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)


class FakeClaudeSDKClient:
    instances: list["FakeClaudeSDKClient"] = []

    def __init__(self, options):
        self.options = options
        self.connected = False
        self.disconnected = False
        self.interrupted = False
        self.sent: list[dict] = []
        self.receive_queue: asyncio.Queue = asyncio.Queue()
        FakeClaudeSDKClient.instances.append(self)

    async def connect(self) -> None:
        self.connected = True

    async def query(self, prompt, session_id: str = "default") -> None:
        async for msg in prompt:
            if "session_id" not in msg:
                msg["session_id"] = session_id
            self.sent.append(msg)

    async def receive_messages(self):
        while True:
            item = await self.receive_queue.get()
            if item is StopAsyncIteration:
                return
            if isinstance(item, Exception):
                raise item
            yield item

    async def interrupt(self) -> None:
        self.interrupted = True

    async def disconnect(self) -> None:
        self.disconnected = True

    async def get_context_usage(self) -> dict:
        return {"totalTokens": 1, "maxTokens": 10, "percentage": 10.0}


async def wait_for(predicate, timeout: float = 1.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out waiting for predicate")


def result(sid: str, text: str = "done") -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=sid,
        total_cost_usd=0.01,
        result=text,
        model_usage={
            "claude-test[200k]": {
                "inputTokens": 10,
                "outputTokens": 3,
                "cacheReadInputTokens": 2,
                "cacheCreationInputTokens": 1,
                "contextWindow": 200000,
                "maxOutputTokens": 32000,
            }
        },
    )


def test_live_session_connect_submit_interrupt_close(monkeypatch, tmp_path):
    async def run():
        FakeClaudeSDKClient.instances.clear()
        monkeypatch.setattr(cc, "ClaudeSDKClient", FakeClaudeSDKClient)
        session = cc.LiveSession(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
            mcp_servers={"tg": {"command": "python", "args": ["tg_mcp.py"]}},
        )

        await session.connect()
        client = FakeClaudeSDKClient.instances[-1]
        assert client.connected
        assert client.options.include_partial_messages is True
        assert client.options.max_turns == 200
        assert client.options.mcp_servers["tg"]["args"] == ["tg_mcp.py"]

        session.submit(
            "look",
            images=[{"media_type": "image/png", "data": "abc"}],
        )
        await wait_for(lambda: len(client.sent) == 1)
        sent = client.sent[0]
        assert sent["session_id"] == "default"
        assert sent["message"]["role"] == "user"
        assert sent["message"]["content"][0]["type"] == "image"
        assert sent["message"]["content"][1] == {"type": "text", "text": "look"}

        await session.interrupt()
        assert client.interrupted
        await session.close()
        assert client.disconnected

    asyncio.run(run())


def test_live_session_events_turn_end_persists_sid(monkeypatch, tmp_path):
    async def run():
        FakeClaudeSDKClient.instances.clear()
        monkeypatch.setattr(cc, "ClaudeSDKClient", FakeClaudeSDKClient)
        session = cc.LiveSession(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)

        await session.connect()
        client = FakeClaudeSDKClient.instances[-1]
        session.submit("hello")
        await wait_for(lambda: len(client.sent) == 1)

        agen = session.events()
        client.receive_queue.put_nowait(
            StreamEvent(
                uuid="u1",
                session_id="sid-1",
                event={
                    "type": "content_block_delta",
                    "delta": {"type": "text_delta", "text": "hel"},
                },
            )
        )
        client.receive_queue.put_nowait(
            AssistantMessage(
                content=[ToolUseBlock(id="t1", name="Read", input={"file_path": "a.py"})],
                model="claude-test",
            )
        )
        client.receive_queue.put_nowait(
            UserMessage(
                content=[
                    ToolResultBlock(
                        tool_use_id="t1",
                        content=[{"type": "text", "text": "boom"}],
                        is_error=True,
                    )
                ]
            )
        )
        client.receive_queue.put_nowait(result("sid-1", "final"))

        events = [await agen.__anext__() for _ in range(5)]
        await agen.aclose()
        await session.close()

        assert [e.kind for e in events] == [
            "text_delta",
            "tool_use",
            "tool_result",
            "session_changed",
            "turn_end",
        ]
        assert events[0].chunk == "hel"
        assert events[1].name == "Read"
        assert events[2].is_error is True
        assert events[-1].response.content == "final"
        state = json.loads((tmp_path / "session.json").read_text())
        assert state["session_id"] == "sid-1"
        assert state["recent_sids"] == ["sid-1"]

    asyncio.run(run())


def test_live_session_reset_drops_pending_inbox(monkeypatch, tmp_path):
    """P1.1: messages queued before /new must NOT be written to the old
    Claude subprocess. Without the drain fix, the input task would flush
    them to the old client before noticing _STOP."""
    async def run():
        FakeClaudeSDKClient.instances.clear()
        monkeypatch.setattr(cc, "ClaudeSDKClient", FakeClaudeSDKClient)
        session = cc.LiveSession(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)

        await session.connect()
        first = FakeClaudeSDKClient.instances[-1]
        # Inject several messages directly into the inbox without yielding
        # control; they remain queued for the input task.
        for i in range(3):
            session._inbox.put_nowait(session._user_message(f"pending-{i}", None))

        await session.reset_live()
        # After reset, the OLD client must not have received any of the pending
        # messages — they belonged to the session V just told us to drop.
        assert first.sent == []
        # And the new client is fresh (no resume).
        assert len(FakeClaudeSDKClient.instances) >= 2
        new_client = FakeClaudeSDKClient.instances[-1]
        assert new_client.options.resume is None

        await session.close()

    asyncio.run(run())


def test_live_session_resume_failure_reconnects_and_replays(monkeypatch, tmp_path):
    async def run():
        FakeClaudeSDKClient.instances.clear()
        monkeypatch.setattr(cc, "ClaudeSDKClient", FakeClaudeSDKClient)
        projects = tmp_path / "projects"
        projects.mkdir()
        old_sid = "old-session"
        (projects / f"{old_sid}.jsonl").write_text(
            "\n".join(
                [
                    json.dumps({"message": {"role": "user", "content": "old question"}}),
                    json.dumps({"message": {"role": "assistant", "content": "old answer"}}),
                ]
            )
        )
        monkeypatch.setattr(cc, "_CC_PROJECTS", projects)
        state_file = tmp_path / "session.json"
        state_file.write_text(json.dumps({"session_id": old_sid, "recent_sids": [old_sid]}))

        session = cc.LiveSession(state_file=state_file, source_prompt="Source: test.")
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)
        await session.connect()
        first = FakeClaudeSDKClient.instances[-1]
        assert first.options.resume == old_sid

        session.submit("new question")
        await wait_for(lambda: len(first.sent) == 1)
        agen = session.events()
        first.receive_queue.put_nowait(Exception("resume failed"))
        first_event = asyncio.create_task(agen.__anext__())

        await wait_for(lambda: len(FakeClaudeSDKClient.instances) == 2)
        second = FakeClaudeSDKClient.instances[-1]
        await wait_for(lambda: len(second.sent) == 1)
        assert second.options.resume is None
        assert "会话从历史归档恢复" in second.options.system_prompt
        assert second.sent[0]["message"]["content"] == "new question"

        second.receive_queue.put_nowait(result("fresh-session", "fresh answer"))
        seen = [await first_event]
        while True:
            ev = await agen.__anext__()
            seen.append(ev)
            if ev.kind == "turn_end":
                break
        await agen.aclose()
        await session.close()

        resp = seen[-1].response
        assert resp.content == "fresh answer"
        assert resp.resume_note and "会话重置" in resp.resume_note

    asyncio.run(run())
