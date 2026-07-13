"""Helpers for resolving user-installed CLI runtimes."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

_AUTO_CLI_VALUES = {"", "auto", "default", "cli-default"}


def env_cli_path(*names: str) -> str | None:
    """Return the first explicit CLI path/command from env.

    A higher-precedence env var set to ``auto`` means "do not pin a path; use
    the normal command lookup". This lets launchd configs opt out of stale
    absolute paths without needing to know the next official install location.
    """
    for name in names:
        raw = os.environ.get(name)
        if raw is None:
            continue
        value = raw.strip()
        if value.lower() in _AUTO_CLI_VALUES:
            return None
        return value
    return None


def resolve_cli_command(fallback: str, *env_names: str) -> str:
    return env_cli_path(*env_names) or fallback


def cli_exists(command: str | None) -> bool:
    value = (command or "").strip()
    if not value:
        return False
    expanded = str(Path(value).expanduser())
    if "/" in expanded:
        return Path(expanded).is_file()
    return shutil.which(expanded) is not None
