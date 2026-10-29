import asyncio
import json

import pytest
from claude_agent_sdk import ResultMessage

import cc


def _disable_blocking_review(monkeypatch, session):
    async def passthrough(response, *, on_stream):
        return response

    monkeypatch.setattr(session, "_apply_blocking_review_gate", passthrough)


def _response(sid: str) -> cc.Response:
    return cc.Response(content=f"answer-{sid}", session_id=sid, cost=0.0, audit={})


def _result_message(sid: str) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=False,
        num_turns=1,
        session_id=sid,
        total_cost_usd=0.0,
        result="done",
        model_usage={},
    )


def test_cc_injects_memory_once_per_real_session(monkeypatch, tmp_path):
    async def run():
        monkeypatch.setenv("BABATA_CC_MEMORY_INJECT", "1")
        monkeypatch.setenv("BABATA_IDLE_RESET_MINUTES", "0")
        rendered = []
        monkeypatch.setattr(
            cc,
            "_render_babata_memory_context",
            lambda source: (
                rendered.append(source)
                or "<memory-context>shared</memory-context>"
            ),
        )

        state_file = tmp_path / "session.json"
        session = cc.CC(
            state_file=state_file,
            source_prompt="Source: test.",
            memory_source="sidebar",
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)
        _disable_blocking_review(monkeypatch, session)
        returned_sids = iter(["sid-1", "sid-1", "sid-2"])
        system_prompts = []

        async def fake_run(opts, *_args):
            system_prompts.append(opts.system_prompt)
            sid = next(returned_sids)
            session._session_id = sid
            session._record_sid(sid)
            return _response(sid)

        monkeypatch.setattr(session, "_run", fake_run)

        await session.query("first")
        await session.query("second")
        session.reset()
        await session.query("third")

        assert ["<memory-context>" in prompt for prompt in system_prompts] == [True, False, True]
        assert rendered == ["sidebar", "sidebar"]
        state = json.loads(state_file.read_text())
        assert state[cc._CLAUDE_MEMORY_INJECTED_KEY] == ["sid-2", "sid-1"]

    asyncio.run(run())


def test_cc_resume_failure_reinjects_for_fresh_session(monkeypatch, tmp_path):
    async def run():
        monkeypatch.setenv("BABATA_CC_MEMORY_INJECT", "1")
        monkeypatch.setenv("BABATA_IDLE_RESET_MINUTES", "0")
        state_file = tmp_path / "session.json"
        state_file.write_text(json.dumps({
            "session_id": "old-sid",
            "recent_sids": ["old-sid"],
            cc._CLAUDE_MEMORY_INJECTED_KEY: ["old-sid"],
        }))
        rendered = []
        monkeypatch.setattr(
            cc,
            "_render_babata_memory_context",
            lambda source: (
                rendered.append(source)
                or "<memory-context>fresh</memory-context>"
            ),
        )
        session = cc.CC(
            state_file=state_file,
            source_prompt="Source: test.",
            memory_source="tg",
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)
        monkeypatch.setattr(session, "_recent_turns_summary", lambda: "")
        _disable_blocking_review(monkeypatch, session)
        system_prompts = []

        async def fake_run(opts, *_args):
            system_prompts.append(opts.system_prompt)
            if len(system_prompts) == 1:
                raise RuntimeError("resume failed")
            session._session_id = "fresh-sid"
            session._record_sid("fresh-sid")
            return _response("fresh-sid")

        monkeypatch.setattr(session, "_run", fake_run)

        response = await session.query("hello")

        assert response.session_id == "fresh-sid"
        assert ["<memory-context>" in prompt for prompt in system_prompts] == [False, True]
        assert rendered == ["tg"]
        state = json.loads(state_file.read_text())
        assert state[cc._CLAUDE_MEMORY_INJECTED_KEY] == ["fresh-sid", "old-sid"]

    asyncio.run(run())


def test_cc_failed_attempt_does_not_mark_memory_injected(monkeypatch, tmp_path):
    async def run():
        monkeypatch.setenv("BABATA_CC_MEMORY_INJECT", "1")
        monkeypatch.setenv("BABATA_IDLE_RESET_MINUTES", "0")
        state_file = tmp_path / "session.json"
        state_file.write_text(json.dumps({"session_id": "old-sid", "recent_sids": ["old-sid"]}))
        monkeypatch.setattr(
            cc,
            "_render_babata_memory_context",
            lambda *_args, **_kwargs: "<memory-context>shared</memory-context>",
        )
        session = cc.CC(state_file=state_file, source_prompt="Source: test.")
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)
        monkeypatch.setattr(session, "_recent_turns_summary", lambda: "")

        async def fail_run(*_args):
            raise RuntimeError("still failed")

        monkeypatch.setattr(session, "_run", fail_run)

        with pytest.raises(RuntimeError, match="still failed"):
            await session.query("hello")

        state = json.loads(state_file.read_text())
        assert cc._CLAUDE_MEMORY_INJECTED_KEY not in state

    asyncio.run(run())


def test_live_session_recovery_marks_only_fresh_result_sid(monkeypatch, tmp_path):
    monkeypatch.setenv("BABATA_CC_MEMORY_INJECT", "1")
    monkeypatch.setenv("BABATA_IDLE_RESET_MINUTES", "0")
    state_file = tmp_path / "session.json"
    state_file.write_text(json.dumps({
        "session_id": "old-sid",
        "recent_sids": ["old-sid"],
        cc._CLAUDE_MEMORY_INJECTED_KEY: ["old-sid"],
    }))
    monkeypatch.setattr(
        cc,
        "_render_babata_memory_context",
        lambda *_args, **_kwargs: "<memory-context>fresh</memory-context>",
    )
    session = cc.LiveSession(state_file=state_file, source_prompt="Source: test.")
    monkeypatch.setattr(session, "_fire_hook", lambda *_: None)
    monkeypatch.setattr(session, "_recent_turns_summary", lambda: "")

    resumed_options = session._make_options()
    assert resumed_options.resume == "old-sid"
    assert "<memory-context>" not in resumed_options.system_prompt

    recovery_prompt = session._prepare_resume_recovery(RuntimeError("resume failed"))
    fresh_options = session._make_options(system_prompt=recovery_prompt, resume=False)
    assert fresh_options.resume is None
    assert "<memory-context>fresh</memory-context>" in fresh_options.system_prompt
    state_before_result = json.loads(state_file.read_text())
    assert state_before_result[cc._CLAUDE_MEMORY_INJECTED_KEY] == ["old-sid"]

    response, changed = session._handle_result_message([_result_message("fresh-sid")], [])
    assert response.session_id == "fresh-sid"
    assert changed == (None, "fresh-sid")
    state_after_result = json.loads(state_file.read_text())
    assert state_after_result[cc._CLAUDE_MEMORY_INJECTED_KEY] == ["fresh-sid", "old-sid"]

    reconnected_options = session._make_options()
    assert reconnected_options.resume == "fresh-sid"
    assert "<memory-context>" not in reconnected_options.system_prompt


def test_cc_memory_opt_out_skips_injection(monkeypatch, tmp_path):
    def fail(*_args, **_kwargs):
        raise AssertionError("memory injection must stay disabled")

    monkeypatch.setattr(cc, "_render_babata_memory_context", fail)
    session = cc.CC(
        state_file=tmp_path / "session.json",
        source_prompt="Source: evidence-bound.",
        memory_enabled=False,
    )

    opts = session._make_query_options(
        include_partial_messages=False,
    )

    assert "Source: evidence-bound." in opts.system_prompt
    assert "<memory-context>" not in opts.system_prompt
    assert session._memory_injected_pending is False
