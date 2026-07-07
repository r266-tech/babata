import asyncio
import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import codex_engine
import engine


class FakeStream:
    def __init__(self, lines: list[str] | None = None, body: bytes = b""):
        self._lines = [line.encode() for line in (lines or [])]
        self._body = body

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)

    async def readline(self):
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self):
        return self._body


class RaisingStream(FakeStream):
    def __init__(self, exc: Exception):
        super().__init__([])
        self._exc = exc

    async def __anext__(self):
        raise self._exc

    async def readline(self):
        raise self._exc


class HangingStream(FakeStream):
    async def readline(self):
        await asyncio.sleep(3600)
        return b""


class CancellableReadStream(FakeStream):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = False

    async def read(self):
        self.started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return b""


class FakeProcess:
    def __init__(self, lines: list[str], returncode: int = 0, stderr: bytes = b""):
        self.stdout = FakeStream(lines)
        self.stderr = FakeStream(body=stderr)
        self._returncode = returncode
        self.terminated = False

    async def wait(self):
        return self._returncode

    def terminate(self):
        self.terminated = True


def _json_line(payload: dict) -> str:
    return json.dumps(payload) + "\n"


def test_codex_command_accumulator_tracks_tool_result_and_usage():
    streamed = []

    async def on_stream(tool_name, tool_input, text_chunk, tool_result):
        streamed.append((tool_name, tool_input, text_chunk, tool_result))

    async def run():
        events = codex_engine.CodexCommandAccumulator(on_stream)
        await events.handle_event({"type": "thread.started", "thread_id": "sid-1"}, "")
        await events.handle_event({
            "type": "item.started",
            "item": {
                "id": "call_0",
                "type": "function_call",
                "name": "browser_tab_list",
                "arguments": {"active": True},
            },
        }, "")
        await events.handle_event({
            "type": "item.completed",
            "item": {
                "call_id": "call_0",
                "type": "function_call_output",
                "output": [{"title": "Home"}],
            },
        }, "")
        await events.handle_event({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "done"},
        }, "")
        await events.handle_event({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 3,
                "cached_input_tokens": 2,
                "output_tokens": 1,
            },
        }, "")

        assert events.result() == {
            "sid": "sid-1",
            "content": "done",
            "tools": ["browser_tab_list"],
            "tool_uses": [{"name": "browser_tab_list"}],
            "usage": {
                "input_tokens": 3,
                "cached_input_tokens": 2,
                "output_tokens": 1,
            },
        }

    asyncio.run(run())

    assert streamed[0][0] == "browser_tab_list"
    assert streamed[1][3] == {"is_error": False, "text": '[{"title": "Home"}]'}


def test_codex_engine_query_parses_json_and_persists(monkeypatch, tmp_path):
    captured = {}
    lines = [
        _json_line({"type": "thread.started", "thread_id": "sid-1"}),
        _json_line({
            "type": "item.started",
            "item": {
                "id": "item_0",
                "type": "command_execution",
                "command": "/bin/zsh -lc pwd",
            },
        }),
        _json_line({
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": "OK"},
        }),
        _json_line({
            "type": "turn.completed",
            "usage": {
                "input_tokens": 10,
                "cached_input_tokens": 4,
                "output_tokens": 2,
            },
        }),
    ]

    async def fake_create(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return FakeProcess(lines)

    async def run():
        monkeypatch.setattr(codex_engine.asyncio, "create_subprocess_exec", fake_create)
        session = codex_engine.CodexEngine(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
            mcp_servers={"tg": {"command": "python", "args": ["tg_mcp.py"], "env": {"S": "x"}}},
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)
        streamed = []
        resp = await session.query(
            "hello",
            on_stream=lambda tool, inp, text, result: streamed.append((tool, text)) or asyncio.sleep(0),
        )
        assert resp.content == "OK"
        assert resp.session_id == "sid-1"
        assert resp.tools == ["/bin/zsh"]
        assert resp.input_tokens == 10
        assert resp.cache_read_tokens == 4
        assert resp.output_tokens == 2
        assert streamed == [("/bin/zsh", None), (None, "OK")]
        cmd_text = " ".join(captured["cmd"])
        assert "mcp_servers.tg" in cmd_text
        assert 'S = "x"' in cmd_text
        assert 'PYTHONDONTWRITEBYTECODE = "1"' in cmd_text
        assert captured["cmd"][-1] == "-"
        assert captured["kwargs"]["stdin"] is codex_engine.asyncio.subprocess.PIPE
        assert captured["kwargs"]["limit"] == codex_engine._CODEX_STREAM_LIMIT
        state = json.loads((tmp_path / "session.json").read_text())
        assert state["session_id"] == "sid-1"
        assert state["recent_sids"] == ["sid-1"]
        assert state["codex_sessions"]["sid-1"]["turns"][-2:] == [["user", "hello"], ["assistant", "OK"]]

    asyncio.run(run())


def test_codex_record_turn_reuses_clean_session_metadata(tmp_path):
    state_file = tmp_path / "session.json"
    state_file.write_text(json.dumps({
        "recent_sids": ["old", 42, "", "sid-1"],
        "engine_session_ids": {"claude": "claude-sid"},
    }))
    session = codex_engine.CodexEngine(
        state_file=state_file,
        source_prompt="Source: test.",
    )
    setattr(session, "_babata_engine_name", "codex")

    session._record_codex_turn("sid-1", "hello", "OK")

    state = json.loads(state_file.read_text())
    assert state["session_id"] == "sid-1"
    assert state["engine_session_ids"] == {"claude": "claude-sid", "codex": "sid-1"}
    assert state["recent_sids"] == ["sid-1", "old"]
    assert "last_activity_at" in state
    rec = state["codex_sessions"]["sid-1"]
    assert rec["turns"] == [["user", "hello"], ["assistant", "OK"]]
    assert "preview" not in rec


def test_codex_record_turn_caps_state_text(tmp_path):
    state_file = tmp_path / "session.json"
    state_file.write_text(json.dumps({
        "codex_sessions": {
            "sid-1": {
                "first_user": "old-" + ("u" * 350) + "-OLD-TAIL",
                "turns": [
                    ["user", "old-" + ("p" * 350) + "-OLD-PROMPT-TAIL"],
                    ["tool", "ignore"],
                    ["assistant", "old-" + ("a" * 350) + "-OLD-ANSWER-TAIL"],
                ],
            },
        },
    }))
    session = codex_engine.CodexEngine(
        state_file=state_file,
        source_prompt="Source: test.",
    )

    session._record_codex_turn(
        "sid-1",
        "new-" + ("q" * 350) + "-NEW-PROMPT-TAIL",
        "reply-" + ("r" * 350) + "-NEW-ANSWER-TAIL",
    )

    raw = state_file.read_text()
    for tail in (
        "OLD-TAIL",
        "OLD-PROMPT-TAIL",
        "OLD-ANSWER-TAIL",
        "NEW-PROMPT-TAIL",
        "NEW-ANSWER-TAIL",
    ):
        assert tail not in raw

    state = json.loads(raw)
    rec = state["codex_sessions"]["sid-1"]
    assert rec["first_user"].endswith("...")
    assert "preview" not in rec
    assert all(len(text) <= codex_engine._CODEX_STATE_TEXT_CHARS + 3 for _, text in rec["turns"])
    assert rec["turns"][-2][0] == "user"
    assert rec["turns"][-1][0] == "assistant"
    rows = session.list_recent_sessions(limit=1)
    assert rows[0]["sid"] == "sid-1"
    assert rows[0]["first_user"] == rec["first_user"]
    assert rows[0]["preview"] == rec["turns"][-1][1]
    assert "OLD-TAIL" not in rows[0]["first_user"]
    assert "NEW-ANSWER-TAIL" not in rows[0]["preview"]
    assert session.get_recent_turns("sid-1", pairs=1, char_cap=1000) == [
        ("user", rec["turns"][-2][1]),
        ("assistant", rec["turns"][-1][1]),
    ]
    setattr(session, "_fire_hook", lambda *_: None)
    assert session.resume("sid-1") is True


def test_codex_engine_injects_babata_memory_once_per_session(monkeypatch, tmp_path):
    monkeypatch.setenv("BABATA_CODEX_MEMORY_INJECT", "1")
    seen_sources: list[str | None] = []
    monkeypatch.setattr(
        codex_engine,
        "_render_babata_memory_context_event",
        lambda source=None, user_prompt=None: (
            seen_sources.append(source) or "<memory-context>shared</memory-context>",
            None,
        ),
    )
    reflex_sources: list[str | None] = []
    monkeypatch.setattr(
        codex_engine,
        "log_memory_reflex_preflight_only",
        lambda source, user_prompt, cpu, cwd: reflex_sources.append((source, cpu, cwd)) or "event-1",
    )
    session = codex_engine.CodexEngine(
        state_file=tmp_path / "session.json",
        source_prompt="Source: test.",
    )

    cmd, prompt_stdin, memory_injected = session._build_command("hello", [], tmp_path / "last.txt")

    assert cmd[-1] == "-"
    assert memory_injected is True
    assert seen_sources == ["unknown"]
    assert "Source: test." in prompt_stdin
    assert "<memory-context>shared</memory-context>" in prompt_stdin
    assert prompt_stdin.endswith("hello")

    session._mark_codex_memory_injected("sid-1")
    session._session_id = "sid-1"
    _cmd, resumed_prompt, resumed_injected = session._build_command("again", [], tmp_path / "last.txt")

    assert resumed_injected is False
    assert "<memory-context>shared</memory-context>" not in resumed_prompt
    assert resumed_prompt.endswith("again")
    assert reflex_sources == [("unknown", "codex", codex_engine._codex_cwd("unknown"))]


def test_codex_engine_uses_explicit_memory_source(monkeypatch, tmp_path):
    monkeypatch.setenv("BABATA_CODEX_MEMORY_INJECT", "1")
    seen_sources: list[str | None] = []
    monkeypatch.setattr(
        codex_engine,
        "_render_babata_memory_context_event",
        lambda source=None, user_prompt=None: (
            seen_sources.append(source) or "<memory-context>shared</memory-context>",
            None,
        ),
    )
    session = codex_engine.CodexEngine(
        state_file=tmp_path / "session.json",
        source_prompt="Source: plain text can change.",
        memory_source="sidebar",
    )

    _cmd, _prompt_stdin, memory_injected = session._build_command("hello", [], tmp_path / "last.txt")

    assert memory_injected is True
    assert seen_sources == ["sidebar"]


def test_codex_build_command_preserves_new_and_resume_cli_flags(monkeypatch, tmp_path):
    monkeypatch.setenv("BABATA_CODEX_CLI_PATH", "codex-bin")
    monkeypatch.setenv("BABATA_CODEX_MODEL", "codex-test")
    monkeypatch.setenv("BABATA_CODEX_IGNORE_USER_CONFIG", "1")
    monkeypatch.setenv("BABATA_CODEX_SEARCH", "1")
    monkeypatch.setenv("BABATA_CODEX_SANDBOX", "read-only")
    monkeypatch.setenv("BABATA_CODEX_MEMORY_INJECT", "0")
    monkeypatch.setattr(
        codex_engine,
        "log_memory_reflex_preflight_only",
        lambda **_kwargs: "event-1",
    )
    session = codex_engine.CodexEngine(
        state_file=tmp_path / "session.json",
        source_prompt="Source: test.",
        memory_source="sidebar",
    )
    image = tmp_path / "image.png"
    last_file = tmp_path / "last.txt"
    common = [
        "codex-bin",
        "-c", "notify=[]",
        "-c", 'approval_policy="never"',
        "-m", "codex-test",
        "--search",
    ]

    cmd, prompt_stdin, memory_injected = session._build_command("hello", [image], last_file)

    assert prompt_stdin == "Source: test.\n\nhello"
    assert memory_injected is False
    assert cmd == [
        *common,
        "exec",
        "--ignore-user-config",
        "--json",
        "--skip-git-repo-check",
        "--sandbox", "read-only",
        "-C", str(Path(codex_engine.__file__).parent),
        "-o", str(last_file),
        "-i", str(image),
        "-",
    ]

    session._session_id = "sid-123"
    resume_cmd, resume_prompt, resume_injected = session._build_command("again", [image], last_file)

    assert resume_prompt == "Source: test.\n\nagain"
    assert resume_injected is False
    assert resume_cmd == [
        *common,
        "exec",
        "resume",
        "--ignore-user-config",
        "--json",
        "--skip-git-repo-check",
        "-o", str(last_file),
        "-i", str(image),
        "sid-123",
        "-",
    ]


def test_codex_engine_streams_tool_results(monkeypatch, tmp_path):
    lines = [
        _json_line({"type": "thread.started", "thread_id": "sid-tools"}),
        _json_line({
            "type": "item.started",
            "item": {
                "id": "call_0",
                "type": "function_call",
                "name": "browser_tab_list",
                "arguments": {"active": True},
            },
        }),
        _json_line({
            "type": "item.completed",
            "item": {
                "id": "output_0",
                "call_id": "call_0",
                "type": "function_call_output",
                "output": [{"title": "X 每日精华 | 2026-05-10"}],
            },
        }),
        _json_line({
            "type": "item.completed",
            "item": {"id": "item_1", "type": "agent_message", "text": "OK"},
        }),
    ]

    async def fake_create(*_cmd, **_kwargs):
        return FakeProcess(lines)

    async def run():
        monkeypatch.setattr(codex_engine.asyncio, "create_subprocess_exec", fake_create)
        session = codex_engine.CodexEngine(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)
        streamed = []

        resp = await session.query(
            "list tabs",
            on_stream=lambda tool, inp, text, result: streamed.append((tool, text, result)) or asyncio.sleep(0),
        )

        assert resp.content == "OK"
        assert resp.tools == ["browser_tab_list"]
        assert streamed[0][0] == "browser_tab_list"
        assert streamed[1][2]["is_error"] is False
        assert "X 每日精华 | 2026-05-10" in streamed[1][2]["text"]
        assert streamed[2] == (None, "OK", None)

    asyncio.run(run())


def test_codex_engine_handles_stdout_reader_splitter_failure(monkeypatch, tmp_path):
    proc = FakeProcess([])
    proc.stdout = RaisingStream(ValueError("Separator is not found, and chunk exceed the limit"))

    async def fake_create(*_cmd, **_kwargs):
        return proc

    async def run():
        monkeypatch.setattr(codex_engine.asyncio, "create_subprocess_exec", fake_create)
        session = codex_engine.CodexEngine(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
        )

        resp = await session.query("hello")

        assert "long-output splitter limit" in resp.content
        assert proc.terminated is True

    asyncio.run(run())


def test_codex_engine_stall_timeout_terminates_process(monkeypatch, tmp_path):
    proc = FakeProcess([])
    proc.stdout = HangingStream()

    async def fake_create(*_cmd, **_kwargs):
        return proc

    async def run():
        monkeypatch.setenv("BABATA_CODEX_STALL_TIMEOUT", "0.01")
        monkeypatch.setattr(codex_engine.asyncio, "create_subprocess_exec", fake_create)
        session = codex_engine.CodexEngine(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
        )

        try:
            await session.query("hello")
        except RuntimeError as e:
            assert "codex stalled" in str(e)
        else:
            raise AssertionError("expected codex stall timeout")

        assert proc.terminated is True

    asyncio.run(run())


def test_codex_run_command_cancellation_cleans_stderr_reader(monkeypatch, tmp_path):
    proc = FakeProcess([])
    proc.stdout = HangingStream()
    stderr = CancellableReadStream()
    proc.stderr = stderr

    async def fake_create(*_cmd, **_kwargs):
        return proc

    async def run():
        monkeypatch.setattr(codex_engine.asyncio, "create_subprocess_exec", fake_create)
        session = codex_engine.CodexEngine(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
        )
        task = asyncio.create_task(session._run_command(["codex"], "hello", None))
        await asyncio.wait_for(stderr.started.wait(), timeout=1)

        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        else:
            raise AssertionError("expected cancelled _run_command task")

        assert proc.terminated is True
        assert stderr.cancelled is True

    asyncio.run(run())


def test_codex_engine_keeps_content_on_splitter_failure(monkeypatch, tmp_path):
    lines = [
        _json_line({"type": "thread.started", "thread_id": "sid-err"}),
        _json_line({
            "type": "item.completed",
            "item": {
                "id": "item_0",
                "type": "agent_message",
                "text": "usable answer",
            },
        }),
    ]

    async def fake_create(*_cmd, **_kwargs):
        return FakeProcess(
            lines,
            returncode=1,
            stderr=b"Separator is not found, and chunk exceed the limit",
        )

    async def run():
        monkeypatch.setattr(codex_engine.asyncio, "create_subprocess_exec", fake_create)
        session = codex_engine.CodexEngine(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)

        resp = await session.query("hello")

        assert resp.content == "usable answer"
        assert resp.session_id == "sid-err"

    asyncio.run(run())


def test_codex_engine_handles_splitter_turn_failed_event(monkeypatch, tmp_path):
    lines = [
        _json_line({"type": "thread.started", "thread_id": "sid-failed"}),
        _json_line({
            "type": "turn.failed",
            "error": {"message": "Separator is found, but chunk is longer than limit"},
        }),
    ]

    async def fake_create(*_cmd, **_kwargs):
        return FakeProcess(lines, returncode=0)

    async def run():
        monkeypatch.setattr(codex_engine.asyncio, "create_subprocess_exec", fake_create)
        session = codex_engine.CodexEngine(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)

        resp = await session.query("hello")

        assert resp.session_id == "sid-failed"
        assert "long-output splitter limit" in resp.content

    asyncio.run(run())


def test_codex_live_session_emits_events(monkeypatch, tmp_path):
    lines = [
        _json_line({"type": "thread.started", "thread_id": "sid-2"}),
        _json_line({
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": "DONE"},
        }),
        _json_line({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
    ]

    async def fake_create(*_cmd, **_kwargs):
        return FakeProcess(lines)

    async def run():
        monkeypatch.setattr(codex_engine.asyncio, "create_subprocess_exec", fake_create)
        session = codex_engine.CodexLiveSession(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)
        await session.connect()
        agen = session.events()
        session.submit("go")
        events = [await agen.__anext__() for _ in range(3)]
        await agen.aclose()
        await session.close()
        assert [e.kind for e in events] == ["text_delta", "session_changed", "turn_end"]
        assert events[0].chunk == "DONE"
        assert events[1].new_sid == "sid-2"
        assert events[2].response.content == "DONE"

    asyncio.run(run())


def test_codex_live_session_interrupt_cancels_active_turn(monkeypatch, tmp_path):
    process = None

    async def fake_create(*_cmd, **_kwargs):
        nonlocal process
        process = FakeProcess([])
        process.stdout = HangingStream()
        return process

    async def run():
        monkeypatch.setattr(codex_engine.asyncio, "create_subprocess_exec", fake_create)
        session = codex_engine.CodexLiveSession(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)
        await session.connect()
        agen = session.events()
        session.submit("go")
        for _ in range(50):
            if process is not None:
                break
            await asyncio.sleep(0.01)
        assert process is not None

        await session.interrupt()

        event = await asyncio.wait_for(agen.__anext__(), timeout=1)
        await agen.aclose()
        await session.close()
        assert event.kind == "turn_end"
        assert "已停止" in event.response.content
        assert event.response.stopped is True
        assert process.terminated is True

    asyncio.run(run())


def test_make_engine_selects_codex(monkeypatch, tmp_path):
    monkeypatch.setenv("BABATA_ENGINE", "codex")
    made = engine.make_engine(
        state_file=tmp_path / "session.json",
        source_prompt="Source: test.",
        live=True,
    )
    assert isinstance(made, codex_engine.CodexLiveSession)


def test_engine_state_overrides_env_and_keeps_engine_specific_sid(monkeypatch, tmp_path):
    state_file = tmp_path / "session.json"
    state_file.write_text(json.dumps({
        "assistant_engine": "codex",
        "session_id": "claude-sid",
        "engine_session_ids": {"codex": "codex-sid"},
    }))
    monkeypatch.setenv("BABATA_ENGINE", "claude")

    made = engine.make_engine(
        state_file=state_file,
        source_prompt="Source: test.",
        live=True,
    )

    assert isinstance(made, codex_engine.CodexLiveSession)
    assert made._session_id == "codex-sid"
    assert made._memory_source == "unknown"


def test_engine_module_has_no_dead_codex_boolean_helper():
    source = Path(engine.__file__).read_text(encoding="utf-8")

    assert "def is_codex_engine" not in source


def test_make_engine_accepts_explicit_memory_source(tmp_path):
    made = engine.make_engine(
        state_file=tmp_path / "session.json",
        source_prompt="Source: wording can change.",
        memory_source="tg",
    )

    assert made._memory_source == "tg"


def test_codex_without_engine_specific_sid_does_not_resume_claude_sid(tmp_path):
    state_file = tmp_path / "session.json"
    state_file.write_text(json.dumps({
        "assistant_engine": "codex",
        "session_id": "claude-sid",
    }))

    made = engine.make_engine(
        state_file=state_file,
        source_prompt="Source: test.",
        live=True,
    )

    assert isinstance(made, codex_engine.CodexLiveSession)
    assert made._session_id is None


def test_claude_record_sid_updates_engine_specific_slot(tmp_path):
    state_file = tmp_path / "session.json"
    made = engine.make_engine(
        state_file=state_file,
        source_prompt="Source: test.",
        live=False,
        engine="claude",
        model="claude-opus-4-7",
    )

    assert made._model == "claude-opus-4-7"
    made._record_sid("claude-sid")

    state = json.loads(state_file.read_text())
    assert state["session_id"] == "claude-sid"
    assert state["engine_session_ids"]["claude"] == "claude-sid"
