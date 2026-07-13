import os
from pathlib import Path

import cli_runtime


def test_env_cli_path_auto_stops_lower_precedence(monkeypatch):
    monkeypatch.setenv("BABATA_CODEX_CLI_PATH", "auto")
    monkeypatch.setenv("CODEX_CLI_PATH", "/stale/codex")

    assert cli_runtime.env_cli_path("BABATA_CODEX_CLI_PATH", "CODEX_CLI_PATH") is None
    assert cli_runtime.resolve_cli_command(
        "codex",
        "BABATA_CODEX_CLI_PATH",
        "CODEX_CLI_PATH",
    ) == "codex"


def test_env_cli_path_returns_explicit_command(monkeypatch):
    monkeypatch.setenv("BABATA_GROK_CLI_PATH", "grok-preview")

    assert cli_runtime.env_cli_path("BABATA_GROK_CLI_PATH", "GROK_CLI_PATH") == "grok-preview"


def test_cli_exists_accepts_executable_path(tmp_path):
    cli = tmp_path / "tool"
    cli.write_text("#!/bin/sh\n")
    cli.chmod(cli.stat().st_mode | os.X_OK)

    assert cli_runtime.cli_exists(str(cli)) is True
    assert cli_runtime.cli_exists(str(Path(tmp_path) / "missing")) is False
