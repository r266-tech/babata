"""Trust controls access without changing Codex's native instruction root."""

from pathlib import Path

import pytest

import codex_engine


@pytest.mark.parametrize("source", ["tg", "wechat", "sidebar"])
@pytest.mark.parametrize("session_id", [None, "existing-session"])
def test_full_trust_keeps_native_repo_scope(monkeypatch, tmp_path, source, session_id):
    monkeypatch.setenv("BABATA_FULL_TRUST", "1")
    monkeypatch.delenv("BABATA_CODEX_SANDBOX", raising=False)
    session = codex_engine.CodexEngine(
        state_file=tmp_path / "session.json",
        source_prompt=f"Source: {source}.",
        memory_source=source,
        memory_enabled=False,
    )
    session._session_id = session_id

    command, prompt, _injected = session._build_command("Review the service.", [], tmp_path / "last.txt")

    assert command[command.index("-C") + 1] == str(Path(codex_engine.__file__).parent)
    assert prompt == f"Source: {source}.\n\nReview the service."
    if session_id:
        # Resume accepts cwd at the parent exec level, not after the subcommand.
        assert command.index("-C") < command.index("resume")
    assert 'sandbox_mode="danger-full-access"' in command
    assert 'approval_policy="never"' in command


@pytest.mark.parametrize("session_id", [None, "existing-session"])
def test_explicit_sandbox_overrides_full_trust_without_changing_scope(monkeypatch, tmp_path, session_id):
    monkeypatch.setenv("BABATA_FULL_TRUST", "1")
    monkeypatch.setenv("BABATA_CODEX_SANDBOX", "read-only")
    session = codex_engine.CodexEngine(
        state_file=tmp_path / "session.json",
        source_prompt="Source: tg.",
        memory_source="tg",
        memory_enabled=False,
    )
    session._session_id = session_id

    command, _prompt, _injected = session._build_command("Review the service.", [], tmp_path / "last.txt")

    assert 'sandbox_mode="read-only"' in command
    assert command[command.index("-C") + 1] == str(Path(codex_engine.__file__).parent)
