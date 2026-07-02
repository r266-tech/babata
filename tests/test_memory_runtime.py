import json
import stat
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import cc
import codex_engine
import memory_runtime


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def test_memory_source_from_prompt_maps_channels(monkeypatch):
    monkeypatch.delenv("BABATA_MEMORY_SOURCE", raising=False)

    assert memory_runtime.memory_source_from_prompt("Source: Telegram. x") == "tg"
    assert memory_runtime.memory_source_from_prompt("Source: WeChat. x") == "wechat"
    assert memory_runtime.memory_source_from_prompt("Source: Sidebar. x") == "sidebar"
    assert memory_runtime.memory_source_from_prompt("Source: test. x") == "unknown"

    monkeypatch.setenv("BABATA_MEMORY_SOURCE", "terminal")
    assert memory_runtime.memory_source_from_prompt("Source: test. x") == "terminal"


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
            "#!/usr/bin/env python3",
            "import json",
            "print(json.dumps({'routes': ['deep'], 'profile': 'deep', 'reasons': ['need detail']}))",
        ]),
    )
    monkeypatch.setenv("BABATA_MEMORY_INJECT_SCRIPT", str(inject_script))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_SCRIPT", str(reflex_script))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_LOG", str(reflex_log))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX", "1")
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_MODE", "enforce")
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_TIMEOUT", "2")
    monkeypatch.delenv("BABATA_MEMORY_PROFILE", raising=False)
    monkeypatch.delenv("BABATA_MEMORY_CPU", raising=False)
    monkeypatch.delenv("BABATA_MEMORY_SOURCE", raising=False)
    monkeypatch.delenv("BABATA_MEMORY_INCLUDE_TOP", raising=False)

    context, event_id = memory_runtime.render_babata_memory_context_event(
        enabled=True,
        source="sidebar",
        user_prompt="hello memory",
        cpu="codex",
        cwd=str(tmp_path),
        timeout=2.0,
    )

    assert event_id
    assert "<memory-context>ok</memory-context>" in context
    assert "<memory-reflex>" in context
    assert "routes: deep" in context
    assert "profile: deep" in context
    assert json.loads(inject_log.read_text()) == {
        "cpu": "codex",
        "include_top": "force",
        "profile": "deep",
        "source": "sidebar",
    }
    events = [json.loads(line) for line in reflex_log.read_text().splitlines()]
    assert events == [
        {
            "actual_profile": "deep",
            "event": "preflight",
            "hint_injected": True,
            "id": event_id,
            "memory_injected": True,
            "message_sha256": events[0]["message_sha256"],
            "message_summary": "hello memory",
            "mode": "enforce",
            "post_answer_observation": "pending",
            "router": {"profile": "deep", "reasons": ["need detail"], "routes": ["deep"]},
            "source": "sidebar",
            "cpu": "codex",
            "ts": events[0]["ts"],
        }
    ]

    memory_runtime.log_memory_reflex_post_answer(event_id, "没有找到")
    events = [json.loads(line) for line in reflex_log.read_text().splitlines()]
    assert events[1]["event"] == "post_answer"
    assert events[1]["id"] == event_id
    assert events[1]["observation"]["memory_miss_marker"] is True


def test_memory_runtime_owns_shared_reflex_helpers():
    cc_source = Path(cc.__file__).read_text(encoding="utf-8")
    codex_source = Path(codex_engine.__file__).read_text(encoding="utf-8")
    forbidden = (
        "_DEFAULT_MEMORY_REFLEX_LOG",
        "def _memory_reflex_enabled",
        "def _memory_reflex_mode",
        "def _memory_reflex_script",
        "def _memory_reflex_timeout",
        "def _format_memory_reflex_hint",
        "def _memory_reflex_log_path",
        "def _message_summary",
        "def _append_memory_reflex_event",
        "def _answer_memory_observation",
    )
    for source in (cc_source, codex_source):
        for marker in forbidden:
            assert marker not in source
