import asyncio
import json
from pathlib import Path

import engine
import grok_engine


class FakeStream:
    def __init__(self, lines: list[str] | None = None, body: bytes = b""):
        self._lines = [line.encode() for line in (lines or [])]
        self._body = body

    async def readline(self):
        if not self._lines:
            return b""
        return self._lines.pop(0)

    async def read(self):
        return self._body


class HangingStream(FakeStream):
    async def readline(self):
        await asyncio.sleep(3600)
        return b""


class FakeProcess:
    def __init__(self, lines: list[str], returncode: int = 0, stderr: bytes = b""):
        self.stdout = FakeStream(lines)
        self.stderr = FakeStream(body=stderr)
        self._returncode = returncode
        self.terminated = False
        self.killed = False

    @property
    def returncode(self):
        return self._returncode if self.terminated or self.killed else None

    async def wait(self):
        return self._returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True


def _json_line(payload: dict) -> str:
    return json.dumps(payload) + "\n"


def test_grok_cli_path_prefers_user_install(monkeypatch, tmp_path):
    monkeypatch.delenv("BABATA_GROK_CLI_PATH", raising=False)
    monkeypatch.delenv("GROK_CLI_PATH", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    grok = tmp_path / ".grok" / "bin" / "grok"
    grok.parent.mkdir(parents=True)
    grok.write_text("#!/bin/sh\n")

    assert grok_engine._grok_cli_path() == str(grok)


def test_grok_command_accumulator_streams_text_and_session():
    streamed = []

    async def on_stream(tool_name, tool_input, text_chunk, tool_result):
        streamed.append((tool_name, tool_input, text_chunk, tool_result))

    async def run():
        events = grok_engine.GrokCommandAccumulator(on_stream)
        await events.handle_line("not json")
        await events.handle_line(_json_line({"type": "text", "data": "O"}))
        await events.handle_line(_json_line({"type": "text", "data": "K"}))
        await events.handle_line(_json_line({
            "type": "end",
            "sessionId": "sid-1",
            "stopReason": "EndTurn",
        }))

        assert events.result() == {
            "sid": "sid-1",
            "content": "OK",
            "tools": [],
            "tool_uses": [],
            "usage": {},
        }

    asyncio.run(run())

    assert streamed == [(None, None, "O", None), (None, None, "K", None)]


def test_grok_model_auto_uses_cli_default(monkeypatch, tmp_path):
    monkeypatch.setenv("BABATA_GROK_CLI_PATH", "grok-bin")
    monkeypatch.setenv("BABATA_GROK_MEMORY_INJECT", "0")
    monkeypatch.setenv("BABATA_GROK_MODEL", "auto")
    session = grok_engine.GrokEngine(
        state_file=tmp_path / "session.json",
        source_prompt="Source: test.",
    )

    cmd, model, injected, prompt_file = session._build_command("hello")

    assert model == "grok-bin"
    assert injected is False
    assert prompt_file is None
    assert "-m" not in cmd
    assert cmd[-2:] == ["-p", "Source: test.\n\nhello"]


def test_grok_engine_query_parses_streaming_json_and_persists(monkeypatch, tmp_path):
    monkeypatch.setenv("BABATA_GROK_CLI_PATH", "grok-bin")
    monkeypatch.setenv("BABATA_GROK_MEMORY_INJECT", "0")
    monkeypatch.setenv("BABATA_GROK_MODEL", "grok-test")
    monkeypatch.setenv("BABATA_GROK_REASONING_EFFORT", "high")
    captured = {}
    lines = [
        "warn: ignored\n",
        _json_line({"type": "text", "data": "O"}),
        _json_line({"type": "text", "data": "K"}),
        _json_line({"type": "end", "sessionId": "sid-1", "stopReason": "EndTurn"}),
    ]

    async def fake_create(*cmd, **kwargs):
        captured["cmd"] = list(cmd)
        captured["kwargs"] = kwargs
        return FakeProcess(lines)

    async def run():
        monkeypatch.setattr(grok_engine.asyncio, "create_subprocess_exec", fake_create)
        monkeypatch.setattr(grok_engine, "run_blocking_review", lambda *_, **__: {"status": "passed"})
        session = grok_engine.GrokEngine(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
            memory_source="sidebar",
        )
        setattr(session, "_babata_engine_name", "grok")
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)
        streamed = []
        resp = await session.query(
            "hello",
            on_stream=lambda tool, inp, text, result: streamed.append((tool, text)) or asyncio.sleep(0),
        )

        assert resp.content == "OK"
        assert resp.session_id == "sid-1"
        assert resp.model == "grok-test"
        assert streamed == [(None, "O"), (None, "K")]
        cmd = captured["cmd"]
        assert cmd[:7] == [
            "grok-bin",
            "--cwd", str(Path(grok_engine.__file__).parent),
            "--output-format", "streaming-json",
            "--permission-mode", "dontAsk",
        ]
        assert "--no-memory" in cmd
        assert "--always-approve" in cmd
        assert ["-m", "grok-test"] == cmd[cmd.index("-m"):cmd.index("-m") + 2]
        assert ["--reasoning-effort", "high"] == cmd[
            cmd.index("--reasoning-effort"):cmd.index("--reasoning-effort") + 2
        ]
        assert cmd[-2] == "-p"
        assert cmd[-1] == "Source: test.\n\nhello"
        assert captured["kwargs"]["stdout"] is grok_engine.asyncio.subprocess.PIPE
        assert captured["kwargs"]["limit"] == grok_engine._GROK_STREAM_LIMIT

        state = json.loads((tmp_path / "session.json").read_text())
        assert state["session_id"] == "sid-1"
        assert state["engine_session_ids"] == {"grok": "sid-1"}
        assert state["recent_sids"] == ["sid-1"]
        assert state["grok_sessions"]["sid-1"]["turns"][-2:] == [["user", "hello"], ["assistant", "OK"]]

    asyncio.run(run())


def test_grok_engine_injects_memory_once_per_session(monkeypatch, tmp_path):
    monkeypatch.setenv("BABATA_GROK_CLI_PATH", "grok-bin")
    monkeypatch.setenv("BABATA_GROK_MEMORY_INJECT", "1")
    seen_sources: list[str | None] = []
    monkeypatch.setattr(
        grok_engine,
        "_render_babata_memory_context_event",
        lambda source=None, user_prompt=None: (
            seen_sources.append(source) or "<memory-context>shared</memory-context>",
            None,
        ),
    )
    reflex_calls = []
    monkeypatch.setattr(
        grok_engine,
        "log_memory_reflex_preflight_only",
        lambda **kwargs: reflex_calls.append(kwargs) or "event-1",
    )
    session = grok_engine.GrokEngine(
        state_file=tmp_path / "session.json",
        source_prompt="Source: test.",
        memory_source="tg",
    )

    cmd, _model, injected, prompt_file = session._build_command("hello")

    assert injected is True
    assert prompt_file is None
    assert seen_sources == ["tg"]
    assert cmd[-2] == "-p"
    assert cmd[-1] == "Source: test.\n\n<memory-context>shared</memory-context>\n\nhello"

    session._mark_grok_memory_injected("sid-1")
    session._session_id = "sid-1"
    resume_cmd, _resume_model, resume_injected, resume_prompt_file = session._build_command("again")

    assert resume_injected is False
    assert resume_prompt_file is None
    assert "--resume" in resume_cmd
    assert resume_cmd[resume_cmd.index("--resume") + 1] == "sid-1"
    assert resume_cmd[-1] == "Source: test.\n\nagain"
    assert reflex_calls == [
        {
            "source": "tg",
            "user_prompt": "again",
            "cpu": "grok",
            "cwd": grok_engine._grok_cwd("tg"),
        }
    ]


def test_make_engine_selects_grok(monkeypatch, tmp_path):
    monkeypatch.setenv("BABATA_ENGINE", "grok")

    made = engine.make_engine(
        state_file=tmp_path / "session.json",
        source_prompt="Source: test.",
        live=True,
    )

    assert isinstance(made, grok_engine.GrokLiveSession)
    assert made.assistant_engine_name == "grok"


def test_grok_without_engine_specific_sid_does_not_resume_claude_sid(tmp_path):
    state_file = tmp_path / "session.json"
    state_file.write_text(json.dumps({
        "assistant_engine": "grok",
        "session_id": "claude-sid",
    }))

    made = engine.make_engine(
        state_file=state_file,
        source_prompt="Source: test.",
        live=True,
    )

    assert isinstance(made, grok_engine.GrokLiveSession)
    assert made._session_id is None


def test_grok_builds_prompt_file_for_images(monkeypatch, tmp_path):
    monkeypatch.setenv("BABATA_GROK_MEMORY_INJECT", "0")
    session = grok_engine.GrokEngine(
        state_file=tmp_path / "session.json",
        source_prompt="Source: test.",
    )

    cmd, _model, _injected, prompt_file = session._build_command(
        "describe",
        images=[{
            "media_type": "image/jpeg",
            "data": "data:image/png;base64,aGVsbG8=",
        }],
    )

    try:
        assert prompt_file is not None
        assert cmd[-2] == "--prompt-file"
        assert cmd[-1] == str(prompt_file)
        assert "-p" not in cmd
        blocks = json.loads(prompt_file.read_text(encoding="utf-8"))
        assert blocks == [
            {"type": "text", "text": "Source: test.\n\ndescribe"},
            {"type": "image", "data": "aGVsbG8=", "mimeType": "image/png"},
        ]
    finally:
        if prompt_file is not None:
            prompt_file.unlink(missing_ok=True)
