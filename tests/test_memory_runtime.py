import subprocess
from types import SimpleNamespace

import pytest

import cc
import codex_engine
import grok_engine
import memory_runtime

def test_default_memory_source_uses_env_not_prompt(monkeypatch):
    monkeypatch.delenv("BABATA_MEMORY_SOURCE", raising=False)

    assert memory_runtime.default_memory_source() == "unknown"

    monkeypatch.setenv("BABATA_MEMORY_SOURCE", "terminal")
    assert memory_runtime.default_memory_source() == "terminal"


def test_memory_inject_enabled_is_owned_by_runtime(monkeypatch):
    for name in (
        "BABATA_CC_MEMORY_INJECT",
        "BABATA_CODEX_MEMORY_INJECT",
        "BABATA_GROK_MEMORY_INJECT",
        "BABATA_CRON_AGENT",
    ):
        monkeypatch.delenv(name, raising=False)

    assert memory_runtime.memory_inject_enabled("claude") is True
    assert memory_runtime.memory_inject_enabled("codex") is True
    assert memory_runtime.memory_inject_enabled("grok") is True

    monkeypatch.setenv("BABATA_CC_MEMORY_INJECT", "0")
    assert memory_runtime.memory_inject_enabled("claude") is False
    assert memory_runtime.memory_inject_enabled("codex") is True
    assert memory_runtime.memory_inject_enabled("grok") is True

    monkeypatch.setenv("BABATA_CODEX_MEMORY_INJECT", "0")
    assert memory_runtime.memory_inject_enabled("codex") is False

    monkeypatch.setenv("BABATA_GROK_MEMORY_INJECT", "0")
    assert memory_runtime.memory_inject_enabled("grok") is False

    monkeypatch.setenv("BABATA_CRON_AGENT", "1")
    assert memory_runtime.memory_inject_enabled("claude") is False
    assert memory_runtime.memory_inject_enabled("codex") is False
    assert memory_runtime.memory_inject_enabled("grok") is False


def test_memory_inject_timeout_is_owned_by_runtime(monkeypatch):
    for name in (
        "BABATA_CC_MEMORY_INJECT_TIMEOUT",
        "BABATA_CODEX_MEMORY_INJECT_TIMEOUT",
        "BABATA_GROK_MEMORY_INJECT_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)

    assert memory_runtime.memory_inject_timeout("claude") == 5.0
    assert memory_runtime.memory_inject_timeout("codex") == 5.0
    assert memory_runtime.memory_inject_timeout("grok") == 5.0

    monkeypatch.setenv("BABATA_CC_MEMORY_INJECT_TIMEOUT", "2.5")
    monkeypatch.setenv("BABATA_CODEX_MEMORY_INJECT_TIMEOUT", "0")
    monkeypatch.setenv("BABATA_GROK_MEMORY_INJECT_TIMEOUT", "3.5")

    assert memory_runtime.memory_inject_timeout("claude") == 2.5
    assert memory_runtime.memory_inject_timeout("codex") == 0.1
    assert memory_runtime.memory_inject_timeout("grok") == 3.5

    monkeypatch.setenv("BABATA_CODEX_MEMORY_INJECT_TIMEOUT", "bad")
    assert memory_runtime.memory_inject_timeout("codex") == 5.0


@pytest.mark.parametrize("profile", [None, "standing", "lite", "recent", "deep", " deep ", "invalid"])
@pytest.mark.parametrize("legacy_mode", [None, "dry-run", "enforce"])
def test_renderer_preserves_profiles_and_ignores_retired_router(monkeypatch, tmp_path, profile, legacy_mode):
    script = tmp_path / "inject.sh"
    script.touch()
    reflex = tmp_path / "reflex-trap"
    reflex.write_text("must never execute")
    event_log = tmp_path / "events.jsonl"
    monkeypatch.setenv("BABATA_MEMORY_INJECT_SCRIPT", str(script))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_SCRIPT", str(reflex))
    monkeypatch.setenv("BABATA_MEMORY_REFLEX_LOG", str(event_log))
    monkeypatch.delenv("BABATA_MEMORY_INCLUDE_TOP", raising=False)
    if profile is None:
        monkeypatch.delenv("BABATA_MEMORY_PROFILE", raising=False)
    else:
        monkeypatch.setenv("BABATA_MEMORY_PROFILE", profile)
    if legacy_mode is None:
        monkeypatch.delenv("BABATA_MEMORY_REFLEX", raising=False)
        monkeypatch.delenv("BABATA_MEMORY_REFLEX_MODE", raising=False)
    else:
        monkeypatch.setenv("BABATA_MEMORY_REFLEX", "1")
        monkeypatch.setenv("BABATA_MEMORY_REFLEX_MODE", legacy_mode)
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        assert command == [str(script)]
        assert "input" not in kwargs
        return SimpleNamespace(returncode=0, stdout="  <memory-context>routes</memory-context>\n", stderr="")

    monkeypatch.setattr(memory_runtime.subprocess, "run", fake_run)
    context = memory_runtime.render_babata_memory_context(enabled=True, source="sidebar", cpu="codex", timeout=2.5)
    assert context == "<memory-context>routes</memory-context>"
    assert len(calls) == 1
    env = calls[0][1]["env"]
    assert env["BABATA_MEMORY_PROFILE"] == (profile.strip() if profile and profile.strip() != "invalid" else "standing")
    assert env["BABATA_MEMORY_CPU"] == "codex"
    assert env["BABATA_MEMORY_SOURCE"] == "sidebar"
    assert env["BABATA_MEMORY_INCLUDE_TOP"] == "skip"
    assert calls[0][1]["timeout"] == 2.5
    assert not event_log.exists()


@pytest.mark.parametrize("module,cpu,flag", [(cc, "claude", "BABATA_CC_MEMORY_INJECT"), (codex_engine, "codex", "BABATA_CODEX_MEMORY_INJECT"), (grok_engine, "grok", "BABATA_GROK_MEMORY_INJECT")])
def test_cpu_adapters_share_context_delivery(monkeypatch, module, cpu, flag):
    monkeypatch.delenv("BABATA_CRON_AGENT", raising=False)
    monkeypatch.setenv(flag, "1")
    monkeypatch.setenv(flag + "_TIMEOUT", "1.5")
    calls = []
    monkeypatch.setattr(module, "render_babata_memory_context", lambda **kwargs: calls.append(kwargs) or "context")
    assert module._render_babata_memory_context("tg") == "context"
    assert calls == [{"enabled": True, "source": "tg", "cpu": cpu, "timeout": 1.5}]


@pytest.mark.parametrize("enabled,present", [(False, False), (False, True), (True, False)])
def test_disabled_or_missing_renderer_skips_subprocess(monkeypatch, tmp_path, enabled, present):
    script = tmp_path / "inject.sh"
    if present:
        script.touch()
    monkeypatch.setenv("BABATA_MEMORY_INJECT_SCRIPT", str(script))

    def fail(*args, **kwargs):
        raise AssertionError("no subprocess should run")

    monkeypatch.setattr(memory_runtime.subprocess, "run", fail)
    assert memory_runtime.render_babata_memory_context(enabled=enabled, source="tg", cpu="claude", timeout=1) == ""


@pytest.mark.parametrize("result", ["nonzero", "empty", "timeout", "oserror"])
def test_failed_renderer_does_not_claim_context(monkeypatch, tmp_path, result):
    script = tmp_path / "inject.sh"
    script.touch()
    monkeypatch.setenv("BABATA_MEMORY_INJECT_SCRIPT", str(script))

    def fake_run(command, **kwargs):
        if result == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        if result == "oserror":
            raise OSError("cannot execute")
        return SimpleNamespace(returncode=2 if result == "nonzero" else 0, stdout="not context" if result == "nonzero" else " \n", stderr="failed" if result == "nonzero" else "")

    monkeypatch.setattr(memory_runtime.subprocess, "run", fake_run)
    assert memory_runtime.render_babata_memory_context(enabled=True, source="tg", cpu="grok", timeout=1) == ""


def test_explicit_include_top_is_preserved(monkeypatch):
    monkeypatch.setenv("BABATA_MEMORY_INCLUDE_TOP", "force")
    assert memory_runtime._memory_inject_env(profile="deep", cpu="claude", source="terminal")["BABATA_MEMORY_INCLUDE_TOP"] == "force"
