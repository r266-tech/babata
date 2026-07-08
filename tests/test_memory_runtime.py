import json
import stat
from pathlib import Path

import cc
import codex_engine
import memory_runtime

_TEST_REFLEX_TIMEOUT_S = "5"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_default_memory_source_uses_env_not_prompt(monkeypatch):
    monkeypatch.delenv("BABATA_MEMORY_SOURCE", raising=False)

    assert memory_runtime.default_memory_source() == "unknown"

    monkeypatch.setenv("BABATA_MEMORY_SOURCE", "terminal")
    assert memory_runtime.default_memory_source() == "terminal"


def test_memory_inject_enabled_is_owned_by_runtime(monkeypatch):
    for name in (
        "BABATA_CC_MEMORY_INJECT",
        "BABATA_CODEX_MEMORY_INJECT",
        "BABATA_CRON_AGENT",
    ):
        monkeypatch.delenv(name, raising=False)

    assert memory_runtime.memory_inject_enabled("claude") is True
    assert memory_runtime.memory_inject_enabled("codex") is True

    monkeypatch.setenv("BABATA_CC_MEMORY_INJECT", "0")
    assert memory_runtime.memory_inject_enabled("claude") is False
    assert memory_runtime.memory_inject_enabled("codex") is True

    monkeypatch.setenv("BABATA_CODEX_MEMORY_INJECT", "0")
    assert memory_runtime.memory_inject_enabled("codex") is False

    monkeypatch.setenv("BABATA_CRON_AGENT", "1")
    assert memory_runtime.memory_inject_enabled("claude") is False
    assert memory_runtime.memory_inject_enabled("codex") is False


def test_memory_inject_timeout_is_owned_by_runtime(monkeypatch):
    for name in (
        "BABATA_CC_MEMORY_INJECT_TIMEOUT",
        "BABATA_CODEX_MEMORY_INJECT_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)

    assert memory_runtime.memory_inject_timeout("claude") == 5.0
    assert memory_runtime.memory_inject_timeout("codex") == 5.0

    monkeypatch.setenv("BABATA_CC_MEMORY_INJECT_TIMEOUT", "2.5")
    monkeypatch.setenv("BABATA_CODEX_MEMORY_INJECT_TIMEOUT", "0")

    assert memory_runtime.memory_inject_timeout("claude") == 2.5
    assert memory_runtime.memory_inject_timeout("codex") == 0.1

    monkeypatch.setenv("BABATA_CODEX_MEMORY_INJECT_TIMEOUT", "bad")
    assert memory_runtime.memory_inject_timeout("codex") == 5.0


def test_memory_reflex_is_opt_in(monkeypatch):
    monkeypatch.delenv("BABATA_MEMORY_REFLEX", raising=False)
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_MODE", "enforce")

    assert memory_runtime._memory_reflex_enabled() is False
    assert memory_runtime.memory_reflex_mode() == "off"

    monkeypatch.setenv("BABATA_MEMORY_REFLEX", "1")
    assert memory_runtime._memory_reflex_enabled() is True
    assert memory_runtime.memory_reflex_mode() == "enforce"


def test_render_memory_context_event_injects_without_default_reflex(monkeypatch, tmp_path):
    inject_script = tmp_path / "inject.sh"
    reflex_script = tmp_path / "reflex.py"
    reflex_log = tmp_path / "events.jsonl"

    _write_executable(
        inject_script,
        "\n".join([
            "#!/bin/sh",
            "printf '%s\\n' '<memory-context>lite</memory-context>'",
        ]),
    )
    _write_executable(
        reflex_script,
        "\n".join([
            "#!/bin/sh",
            "printf '%s\\n' '{\"routes\":[\"deep\"],\"profile\":\"deep\"}'",
        ]),
    )
    monkeypatch.setenv("BABATA_MEMORY_INJECT_SCRIPT", str(inject_script))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_SCRIPT", str(reflex_script))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_LOG", str(reflex_log))
    monkeypatch.delenv("BABATA_MEMORY_REFLEX", raising=False)
    monkeypatch.delenv("BABATA_MEMORY_PROFILE", raising=False)

    context, event_id = memory_runtime.render_babata_memory_context_event(
        enabled=True,
        source="sidebar",
        user_prompt="hello memory",
        cpu="codex",
        cwd=str(tmp_path),
        timeout=float(_TEST_REFLEX_TIMEOUT_S),
    )

    assert context == "<memory-context>lite</memory-context>"
    assert event_id is None
    assert not reflex_log.exists()


def test_log_memory_reflex_preflight_only_skips_when_default_off(monkeypatch, tmp_path):
    reflex_script = tmp_path / "reflex.py"
    reflex_log = tmp_path / "events.jsonl"
    _write_executable(
        reflex_script,
        "\n".join([
            "#!/bin/sh",
            "printf '%s\\n' '{\"routes\":[\"recent\"],\"profile\":\"recent\"}'",
        ]),
    )
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_SCRIPT", str(reflex_script))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_LOG", str(reflex_log))
    monkeypatch.delenv("BABATA_MEMORY_REFLEX", raising=False)

    event_id = memory_runtime.log_memory_reflex_preflight_only(
        source="sidebar",
        user_prompt="look up yesterday",
        cpu="codex",
        cwd=str(tmp_path),
    )

    assert event_id is None
    assert not reflex_log.exists()


def test_render_memory_context_event_logs_enforced_reflex(monkeypatch, tmp_path):
    inject_log = tmp_path / "inject-env.json"
    inject_script = tmp_path / "inject.sh"
    reflex_script = tmp_path / "reflex.py"
    reflex_log = tmp_path / "events.jsonl"

    _write_executable(
        inject_script,
        "\n".join([
            "#!/usr/bin/env python3",
            "import json, os",
            f"open({str(inject_log)!r}, 'w').write(json.dumps({{",
            "    'profile': os.environ.get('BABATA_MEMORY_PROFILE'),",
            "    'cpu': os.environ.get('BABATA_MEMORY_CPU'),",
            "    'source': os.environ.get('BABATA_MEMORY_SOURCE'),",
            "    'include_top': os.environ.get('BABATA_MEMORY_INCLUDE_TOP'),",
            "}, sort_keys=True))",
            "print('<memory-context>ok</memory-context>')",
        ]),
    )
    _write_executable(
        reflex_script,
        "\n".join([
            "#!/bin/sh",
            f"printf '%s\\n' '{{\"routes\":[\"deep\"],\"profile\":\"deep\",\"flags\":[\"reflection_candidate\"],\"reasons\":[\"need detail\"],\"cwd\":\"{str(tmp_path)}\",\"source\":\"sidebar\"}}'",
        ]),
    )
    monkeypatch.setenv("BABATA_MEMORY_INJECT_SCRIPT", str(inject_script))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_SCRIPT", str(reflex_script))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_LOG", str(reflex_log))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX", "1")
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_MODE", "enforce")
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_TIMEOUT", _TEST_REFLEX_TIMEOUT_S)
    monkeypatch.delenv("BABATA_MEMORY_PROFILE", raising=False)
    monkeypatch.setenv("BABATA_MEMORY_CPU", "stale-cpu")
    monkeypatch.setenv("BABATA_MEMORY_SOURCE", "stale-source")
    monkeypatch.delenv("BABATA_MEMORY_INCLUDE_TOP", raising=False)

    context, event_id = memory_runtime.render_babata_memory_context_event(
        enabled=True,
        source="sidebar",
        user_prompt="hello memory",
        cpu="codex",
        cwd=str(tmp_path),
        timeout=float(_TEST_REFLEX_TIMEOUT_S),
    )

    assert event_id
    assert "<memory-context>ok</memory-context>" in context
    assert "<memory-reflex>" not in context
    assert "routes: deep" not in context
    assert "profile: deep" not in context
    assert "need detail" not in context
    assert "signal:" not in context
    assert "why:" not in context
    assert json.loads(inject_log.read_text()) == {
        "cpu": "codex",
        "include_top": "skip",
        "profile": "deep",
        "source": "sidebar",
    }
    events = [json.loads(line) for line in reflex_log.read_text().splitlines()]
    assert events == [
        {
            "actual_profile": "deep",
            "event": "preflight",
            "id": event_id,
            "memory_injected": True,
            "message_sha256": events[0]["message_sha256"],
            "mode": "enforce",
            "post_answer_observation": "pending",
            "router": {"flags": ["reflection_candidate"], "profile": "deep", "routes": ["deep"]},
            "source": "sidebar",
            "cpu": "codex",
            "ts": events[0]["ts"],
        }
    ]
    assert len(events[0]["message_sha256"]) == 64
    assert "hello memory" not in reflex_log.read_text(encoding="utf-8")
    assert "need detail" not in reflex_log.read_text(encoding="utf-8")
    assert str(tmp_path) not in reflex_log.read_text(encoding="utf-8")
    assert "message_summary" not in events[0]

    memory_runtime.log_memory_reflex_post_answer(event_id, "没有找到")
    events = [json.loads(line) for line in reflex_log.read_text().splitlines()]
    assert events[1]["event"] == "post_answer"
    assert events[1]["id"] == event_id
    assert len(events[1]["answer_sha256"]) == 64
    assert events[1]["observation"]["memory_miss_marker"] is True
    assert "answer_summary" not in events[1]
    assert "没有找到" not in reflex_log.read_text(encoding="utf-8")


def test_render_memory_context_event_preserves_explicit_profile_and_include_top(monkeypatch, tmp_path):
    inject_log = tmp_path / "inject-env.json"
    inject_script = tmp_path / "inject.sh"
    reflex_script = tmp_path / "reflex.py"
    reflex_log = tmp_path / "events.jsonl"

    _write_executable(
        inject_script,
        "\n".join([
            "#!/usr/bin/env python3",
            "import json, os",
            f"open({str(inject_log)!r}, 'w').write(json.dumps({{",
            "    'profile': os.environ.get('BABATA_MEMORY_PROFILE'),",
            "    'include_top': os.environ.get('BABATA_MEMORY_INCLUDE_TOP'),",
            "}, sort_keys=True))",
            "print('<memory-context>explicit</memory-context>')",
        ]),
    )
    _write_executable(
        reflex_script,
        "\n".join([
            "#!/bin/sh",
            "printf '%s\\n' '{\"routes\":[\"deep\"],\"profile\":\"deep\"}'",
        ]),
    )
    monkeypatch.setenv("BABATA_MEMORY_INJECT_SCRIPT", str(inject_script))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_SCRIPT", str(reflex_script))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_LOG", str(reflex_log))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX", "1")
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_MODE", "enforce")
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_TIMEOUT", _TEST_REFLEX_TIMEOUT_S)
    monkeypatch.setenv("BABATA_MEMORY_PROFILE", "recent")
    monkeypatch.setenv("BABATA_MEMORY_INCLUDE_TOP", "force")

    context, event_id = memory_runtime.render_babata_memory_context_event(
        enabled=True,
        source="terminal",
        user_prompt="need deep",
        cpu="codex",
        cwd=str(tmp_path),
        timeout=float(_TEST_REFLEX_TIMEOUT_S),
    )

    assert event_id
    assert "<memory-context>explicit</memory-context>" in context
    assert json.loads(inject_log.read_text()) == {
        "include_top": "force",
        "profile": "recent",
    }
    event = json.loads(reflex_log.read_text().splitlines()[0])
    assert event["actual_profile"] == "recent"
    assert event["router"] == {"profile": "deep", "routes": ["deep"]}


def test_log_memory_reflex_preflight_only_records_router_without_inject(monkeypatch, tmp_path):
    reflex_script = tmp_path / "reflex.py"
    reflex_log = tmp_path / "events.jsonl"
    _write_executable(
        reflex_script,
        "\n".join([
            "#!/bin/sh",
            f"printf '%s\\n' '{{\"routes\":[\"recent\"],\"profile\":\"recent\",\"reasons\":[\"history\"],\"cwd\":\"{str(tmp_path)}\"}}'",
        ]),
    )
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_SCRIPT", str(reflex_script))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_LOG", str(reflex_log))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX", "1")
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_MODE", "dry-run")
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_TIMEOUT", _TEST_REFLEX_TIMEOUT_S)
    monkeypatch.delenv("BABATA_MEMORY_PROFILE", raising=False)

    event_id = memory_runtime.log_memory_reflex_preflight_only(
        source="sidebar",
        user_prompt="look up yesterday",
        cpu="codex",
        cwd=str(tmp_path),
    )

    assert event_id
    events = [json.loads(line) for line in reflex_log.read_text().splitlines()]
    assert events == [
        {
            "actual_profile": "lite",
            "event": "preflight",
            "id": event_id,
            "memory_injected": False,
            "message_sha256": events[0]["message_sha256"],
            "mode": "dry-run",
            "post_answer_observation": "pending",
            "router": {"profile": "recent", "routes": ["recent"]},
            "source": "sidebar",
            "cpu": "codex",
            "ts": events[0]["ts"],
        }
    ]
    assert len(events[0]["message_sha256"]) == 64
    assert "message_summary" not in events[0]
    assert "look up yesterday" not in reflex_log.read_text(encoding="utf-8")
    assert "history" not in reflex_log.read_text(encoding="utf-8")
    assert str(tmp_path) not in reflex_log.read_text(encoding="utf-8")


def test_memory_reflex_routes_stay_telemetry_not_prompt_hints(monkeypatch, tmp_path):
    inject_script = tmp_path / "inject.sh"
    reflex_script = tmp_path / "reflex.py"
    reflex_log = tmp_path / "events.jsonl"
    _write_executable(
        inject_script,
        "\n".join([
            "#!/bin/sh",
            "printf '%s\\n' '<memory-context>ok</memory-context>'",
        ]),
    )
    _write_executable(
        reflex_script,
        "\n".join([
            "#!/bin/sh",
            "printf '%s\\n' '{\"routes\":[\"brain\",\"wx\",\"code-grounded\"],\"profile\":\"lite\",\"reasons\":[\"private detail\"]}'",
        ]),
    )
    monkeypatch.setenv("BABATA_MEMORY_INJECT_SCRIPT", str(inject_script))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_SCRIPT", str(reflex_script))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_LOG", str(reflex_log))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX", "1")
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_MODE", "enforce")
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_TIMEOUT", _TEST_REFLEX_TIMEOUT_S)

    context, event_id = memory_runtime.render_babata_memory_context_event(
        enabled=True,
        source="sidebar",
        user_prompt="查一下这个人的微信和资料",
        cpu="codex",
        cwd=str(tmp_path),
        timeout=float(_TEST_REFLEX_TIMEOUT_S),
    )

    assert event_id
    assert context == "<memory-context>ok</memory-context>"
    assert "<memory-reflex>" not in context
    assert "routes:" not in context
    assert "private detail" not in context
    event = json.loads(reflex_log.read_text().splitlines()[0])
    assert event["router"] == {"profile": "lite", "routes": ["brain", "wx", "code-grounded"]}
    assert "hint_injected" not in event


def test_memory_reflex_values_are_allowlisted(monkeypatch, tmp_path):
    reflex_log = tmp_path / "events.jsonl"
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_LOG", str(reflex_log))

    event_id = memory_runtime._log_memory_reflex_preflight(
        reflex={
            "routes": ["recent", str(tmp_path), "unexpected-route"],
            "profile": str(tmp_path),
            "route": "deep",
            "flags": ["reflection_candidate", str(tmp_path), "free text reason"],
        },
        user_prompt="hello",
        source="sidebar",
        cpu="codex",
        mode="dry-run",
        actual_profile="lite",
        memory_injected=False,
    )

    assert event_id
    text = reflex_log.read_text(encoding="utf-8")
    event = json.loads(text.splitlines()[0])
    assert event["router"] == {
        "flags": ["reflection_candidate"],
        "routes": ["recent"],
    }
    assert str(tmp_path) not in text


def test_memory_runtime_owns_shared_reflex_helpers():
    cc_source = Path(cc.__file__).read_text(encoding="utf-8")
    codex_source = Path(codex_engine.__file__).read_text(encoding="utf-8")
    forbidden = (
        "def _cc_memory_inject_enabled",
        "def _codex_memory_inject_enabled",
        "def _memory_inject_timeout",
        "_DEFAULT_MEMORY_REFLEX_LOG",
        "def _memory_source_from_prompt",
        "def _memory_reflex_for_prompt",
        "def _memory_reflex_enabled",
        "def _memory_reflex_mode",
        "def _memory_reflex_script",
        "def _memory_reflex_timeout",
        "def _render_babata_memory_context(",
        "def _memory_reflex_log_path",
        "def _append_memory_reflex_event",
        "def _log_memory_reflex_preflight_only",
        "def _answer_memory_observation",
    )
    for source in (cc_source, codex_source):
        for marker in forbidden:
            assert marker not in source
