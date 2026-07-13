import asyncio
import json
from pathlib import Path

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
        if hasattr(prompt, "__aiter__"):
            async for msg in prompt:
                if "session_id" not in msg:
                    msg["session_id"] = session_id
                self.sent.append(msg)
        else:
            self.sent.append(prompt)

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


def test_user_message_payload_and_sdk_event_parser_share_cpu_boundary():
    payload = cc._user_message_payload(
        "look",
        [{"media_type": "image/png", "data": "abc"}],
    )
    assert payload["message"]["role"] == "user"
    assert payload["message"]["content"][0]["source"]["media_type"] == "image/png"
    assert payload["message"]["content"][1] == {"type": "text", "text": "look"}

    tools_seen: list[str] = []
    events = []
    for msg in [
        StreamEvent(
            uuid="u1",
            session_id="sid-1",
            event={
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hel"},
            },
        ),
        AssistantMessage(
            content=[ToolUseBlock(id="t1", name="Read", input={"file_path": "a.py"})],
            model="claude-test",
        ),
        UserMessage(
            content=[
                ToolResultBlock(
                    tool_use_id="t1",
                    content=[{"type": "text", "text": "boom"}],
                    is_error=True,
                )
            ]
        ),
    ]:
        events.extend(cc._events_from_sdk_message(msg, tools_seen))

    assert [event.kind for event in events] == ["text_delta", "tool_use", "tool_result"]
    assert events[0].chunk == "hel"
    assert events[1].name == "Read"
    assert events[1].input_dict == {"file_path": "a.py"}
    assert events[2].is_error is True
    assert events[2].text == "boom"
    assert tools_seen == ["Read"]

    response = cc._response_from_messages([result("sid-1", "final")], ["Read"])
    assert response.content == "final"
    assert response.model == "claude-test[200k]"
    assert response.input_tokens == 10
    assert response.cache_read_tokens == 2


def test_claude_transport_loads_repo_instructions_without_user_profile():
    assert cc._SETTING_SOURCES[0] == "project"
    repo_claude = (Path(cc.__file__).parent / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Raw records and archives are append-only." in repo_claude
    assert "scripts/self-ops.sh" in repo_claude


def test_full_trust_keeps_repo_instructions_when_cwd_moves_home(monkeypatch, tmp_path):
    monkeypatch.setattr(cc, "_FULL_TRUST", True)
    monkeypatch.setattr(cc, "_DEFAULT_CWD", str(tmp_path))
    session = cc.CC(
        state_file=tmp_path / "session.json",
        source_prompt="Source: test.",
        memory_enabled=False,
    )

    opts = session._make_query_options(
        user_prompt="hello",
        include_partial_messages=False,
    )

    assert opts.cwd == str(tmp_path)
    assert "<babata-repo-instructions>" in opts.system_prompt
    assert "scripts/self-ops.sh" in opts.system_prompt
    assert "Raw records and archives are append-only." in opts.system_prompt


def test_full_trust_one_shot_resume_retry_keeps_repo_instructions(monkeypatch, tmp_path):
    async def run():
        monkeypatch.setattr(cc, "_FULL_TRUST", True)
        monkeypatch.setattr(cc, "_DEFAULT_CWD", str(tmp_path))
        monkeypatch.setenv("BABATA_IDLE_RESET_MINUTES", "0")
        state_file = tmp_path / "session.json"
        state_file.write_text(
            json.dumps({"session_id": "old-session", "recent_sids": ["old-session"]}),
            encoding="utf-8",
        )
        session = cc.CC(
            state_file=state_file,
            source_prompt="Source: test.",
            memory_enabled=False,
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)
        monkeypatch.setattr(session, "_recent_turns_summary", lambda: "restored history")
        prompts = []

        async def fake_run(opts, *_args):
            prompts.append(opts.system_prompt)
            if len(prompts) == 1:
                raise RuntimeError("resume failed")
            return cc.Response(content="ok", session_id="fresh-session", cost=0.0)

        monkeypatch.setattr(session, "_run", fake_run)

        response = await session.query("hello")

        assert response.content == "ok"
        assert len(prompts) == 2
        assert all("<babata-repo-instructions>" in prompt for prompt in prompts)
        assert "restored history" in prompts[1]
        assert "scripts/self-ops.sh" in prompts[1]

    asyncio.run(run())


def test_cc_query_one_shot_uses_shared_stream_and_response(monkeypatch, tmp_path):
    async def run():
        FakeClaudeSDKClient.instances.clear()
        monkeypatch.setattr(cc, "ClaudeSDKClient", FakeClaudeSDKClient)
        session = cc.CC(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)

        streamed = []

        async def on_stream(tool_name, tool_input, text_chunk, tool_result):
            streamed.append((tool_name, tool_input, text_chunk, tool_result))

        task = asyncio.create_task(session.query("hello", on_stream=on_stream))
        await wait_for(lambda: len(FakeClaudeSDKClient.instances) == 1)
        client = FakeClaudeSDKClient.instances[-1]
        await wait_for(lambda: client.sent == ["hello"])

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

        response = await task

        assert response.content == "final"
        assert response.session_id == "sid-1"
        assert response.tools == ["Read"]
        assert response.model == "claude-test[200k]"
        assert streamed == [
            (None, None, "hel", None),
            ("Read", {"file_path": "a.py"}, None, None),
            (None, None, None, {"is_error": True, "text": "boom"}),
        ]
        assert client.disconnected is True

    asyncio.run(run())


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
        assert client.options.setting_sources[0] == "project"
        assert client.options.mcp_servers["tg"]["args"] == ["tg_mcp.py"]
        assert client.options.mcp_servers["tg"]["env"]["PYTHONDONTWRITEBYTECODE"] == "1"

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


def test_mcp_server_env_forces_no_repo_bytecode(tmp_path):
    original = {
        "tg": {
            "command": "python",
            "args": ["tg_mcp.py"],
            "env": {"PYTHONDONTWRITEBYTECODE": "0", "KEEP": "x"},
        }
    }
    session = cc.CC(
        state_file=tmp_path / "session.json",
        source_prompt="Source: test.",
        mcp_servers=original,
    )

    assert session._mcp_servers["tg"]["env"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert session._mcp_servers["tg"]["env"]["KEEP"] == "x"
    assert original["tg"]["env"]["PYTHONDONTWRITEBYTECODE"] == "0"


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
        # messages — they belonged to the session the user just told us to drop.
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
                    json.dumps({"message": {"role": "user", "content": "<command-name>/status</command-name>"}}),
                    json.dumps({"message": {"role": "assistant", "content": "status output"}}),
                    json.dumps({"message": {"role": "user", "content": "old question"}}),
                    json.dumps({"message": {"role": "user", "content": "<bash-input>pwd</bash-input>"}}),
                    json.dumps({"message": {"role": "assistant", "content": "/tmp/internal-output"}}),
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
        assert "user: old question" in second.options.system_prompt
        assert "assistant: old answer" in second.options.system_prompt
        assert "<command-name>" not in second.options.system_prompt
        assert "<bash-input>" not in second.options.system_prompt
        assert "status output" not in second.options.system_prompt
        assert "/tmp/internal-output" not in second.options.system_prompt
        assert "V: old question" not in second.options.system_prompt
        assert "CC: old answer" not in second.options.system_prompt
        assert len(second.options.system_prompt) <= 1400
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
