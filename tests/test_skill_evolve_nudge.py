import json
import subprocess

import skill_evolve_nudge


def test_skill_evolve_nudge_is_opt_in_by_default(monkeypatch, tmp_path):
    script = tmp_path / "nudge.sh"
    script.write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setenv("SKILL_EVOLVE_NUDGE_SCRIPT", str(script))
    monkeypatch.delenv("BABATA_SKILL_EVOLVE_NUDGE", raising=False)
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))

    skill_evolve_nudge.notify_skill_evolve_turn(
        session_id="sid",
        cpu="codex",
        source="sidebar",
        channel="Sidebar",
    )

    assert calls == []


def test_skill_evolve_nudge_runs_when_explicitly_enabled(monkeypatch, tmp_path):
    script = tmp_path / "nudge.sh"
    script.write_text("#!/bin/bash\n", encoding="utf-8")
    monkeypatch.setenv("SKILL_EVOLVE_NUDGE_SCRIPT", str(script))
    monkeypatch.setenv("BABATA_SKILL_EVOLVE_NUDGE", "1")
    calls = []
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))

    skill_evolve_nudge.notify_skill_evolve_turn(
        session_id="sid",
        cpu="codex",
        source="sidebar",
        channel="Sidebar",
        metadata={"tools": ["page_snapshot"]},
    )

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == ["/bin/bash", str(script)]
    payload = json.loads(kwargs["env"]["SKILL_EVOLVE_NUDGE_JSON"])
    assert payload["session_id"] == "sid"
    assert payload["metadata"] == {"tools": ["page_snapshot"]}
