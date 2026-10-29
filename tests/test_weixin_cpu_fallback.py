import asyncio
import importlib
import sys

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

import cc as cc_mod


def _result(is_error, text):
    return ResultMessage(
        subtype="success", duration_ms=1, duration_api_ms=1, is_error=is_error,
        num_turns=1, session_id="sid-1", result=text,
    )


def _run_claude(monkeypatch, tmp_path, messages):
    class FakeClient:
        def __init__(self, opts):
            pass

        async def connect(self):
            pass

        async def disconnect(self):
            pass

        async def query(self, prompt):
            pass

        async def receive_messages(self):
            for msg in messages:
                yield msg

    monkeypatch.setattr(cc_mod, "ClaudeSDKClient", FakeClient)
    monkeypatch.setattr(cc_mod.CC, "_fire_hook", lambda *a, **k: None)
    session = cc_mod.CC(
        state_file=tmp_path / "s.json", source_prompt="Source: test.",
        effort="medium", memory_enabled=False,
    )
    streamed = []

    async def on_stream(name, inp, chunk, result):
        if chunk:
            streamed.append(chunk)

    opts = session._make_query_options(include_partial_messages=False)
    resp = asyncio.run(session._run(opts, "hi", None, on_stream))
    return opts, resp, streamed


@pytest.mark.parametrize("error", ["rate_limit", "billing_error", "authentication_failed"])
def test_claude_limit_notice_marks_cpu_unavailable_without_streaming(monkeypatch, tmp_path, error):
    notice = "You've hit your limit"
    opts, resp, streamed = _run_claude(monkeypatch, tmp_path, [
        AssistantMessage(content=[TextBlock(notice)], model="m", error=error),
        _result(True, notice),
    ])

    assert opts.effort == "medium"
    assert resp.cpu_unavailable
    assert streamed == []


def test_recovered_claude_notice_is_not_a_failed_turn(monkeypatch, tmp_path):
    _, resp, _ = _run_claude(monkeypatch, tmp_path, [
        AssistantMessage(content=[TextBlock("API Error: 429")], model="m", error="rate_limit"),
        AssistantMessage(content=[TextBlock("answer")], model="m"),
        _result(False, "answer"),
    ])

    assert not resp.cpu_unavailable
    assert resp.content == "answer"


@pytest.fixture
def weixin_bot(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("BABATA_PRIMARY_CPU", "claude")
    monkeypatch.setenv("BABATA_FALLBACK_CPU", "codex")
    sys.modules.pop("weixin_bot", None)
    module = importlib.import_module("weixin_bot")
    yield module
    sys.modules.pop("weixin_bot", None)


class _Resp:
    def __init__(self, content, unavailable=False):
        self.content = content
        self.cpu_unavailable = unavailable


class _Engine:
    def __init__(self, resp):
        self.resp = resp
        self.calls = 0

    async def query(self, *args, **kwargs):
        self.calls += 1
        return self.resp


class _Coalescer:
    on_stream = None

    def reset_for_replay(self):
        pass


def test_weixin_cpus_keep_separate_state(weixin_bot):
    assert weixin_bot.cc._state_file.name.endswith("-weixin-claude-session.json")
    assert weixin_bot.fallback_cc._state_file.name.endswith("-weixin-codex-session.json")


@pytest.mark.parametrize("unavailable,expected", [(True, "fallback"), (False, "primary")])
def test_weixin_turn_falls_back_only_when_primary_unavailable(
    weixin_bot, monkeypatch, unavailable, expected,
):
    primary = _Engine(_Resp("primary", unavailable))
    fallback = _Engine(_Resp("fallback"))
    monkeypatch.setattr(weixin_bot, "cc", primary)
    monkeypatch.setattr(weixin_bot, "fallback_cc", fallback)

    resp = asyncio.run(weixin_bot._query_wx_turn_with_recovery("hi", [], _Coalescer()))

    assert resp.content == expected
    assert fallback.calls == int(unavailable)


def test_weixin_turn_without_fallback_tells_user_in_chinese(weixin_bot, monkeypatch):
    primary = _Engine(_Resp("You've hit your limit", True))
    monkeypatch.setattr(weixin_bot, "cc", primary)
    monkeypatch.setattr(weixin_bot, "fallback_cc", None)

    resp = asyncio.run(weixin_bot._query_wx_turn_with_recovery("hi", [], _Coalescer()))

    assert resp.content.startswith("⚠️ Claude 暂时不可用")
    assert resp.content.endswith("You've hit your limit")
    assert primary.calls == 1
