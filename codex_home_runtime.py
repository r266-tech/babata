#!/usr/bin/env python3
"""Materialize a scoped Codex home for headless Babata CPU runs.

The desktop Codex app owns the live auth/config state under ~/.codex. Cron jobs
run with a separate CODEX_HOME so they do not write into the interactive app
home. This helper copies only the small files needed for `codex exec` to start.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import sys
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

REQUIRED_FILES = ("config.toml",)
OPTIONAL_FILES = (
    "auth.json",
    "provider-slots.json",
    "models_cache.json",
    "version.json",
)
# Top-level config keys whose relative file values Codex resolves against
# CODEX_HOME. A verbatim config copy must bring the referenced file along, or
# `codex exec` exits at startup with a bare ENOENT.
CONFIG_RELATIVE_FILE_KEYS = ("model_catalog_json",)
MATERIALIZE_MODE_FULL = "full"
MATERIALIZE_MODE_RESTRICTED_PROVIDER = "restricted-provider"
MATERIALIZE_MODES = (
    MATERIALIZE_MODE_FULL,
    MATERIALIZE_MODE_RESTRICTED_PROVIDER,
)
RESTRICTED_PROVIDER_REQUIRED_FIELDS = (
    "name",
    "base_url",
    "experimental_bearer_token",
    "wire_api",
    "requires_openai_auth",
)
RESTRICTED_PROVIDER_OPTIONAL_FIELDS = ("supports_standalone_web_search",)
RESTRICTED_PROVIDER_FIELDS = (
    *RESTRICTED_PROVIDER_REQUIRED_FIELDS,
    *RESTRICTED_PROVIDER_OPTIONAL_FIELDS,
)
RESTRICTED_HOME_MARKER = ".babata-restricted-provider-home"
RESTRICTED_FORBIDDEN_ENTRIES = frozenset(
    {
        ".agents",
        ".codex",
        "AGENTS.md",
        "auth.json",
        "hooks.json",
        "plugins",
        "provider-slots.json",
        "rules",
    }
)
RESTRICTED_RUNTIME_DIRECTORIES = frozenset(
    {
        "log",
        "sessions",
        "shell_snapshots",
        "skills",
        "thread-writer-locks",
        "tmp",
    }
)
RESTRICTED_RUNTIME_DIRECTORY_INPUT_MODES = frozenset({0o700, 0o755})
RESTRICTED_RUNTIME_FILE_RE = re.compile(
    r"^(?:goals|logs|memories|queue|state|thread_history)_[1-9][0-9]*"
    r"\.sqlite(?:-(?:shm|wal))?$"
)


class MaterializationError(RuntimeError):
    """The requested Codex home cannot satisfy its materialization policy."""


def _atomic_copy(src: Path, dest: Path, mode: int) -> None:
    tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
    try:
        shutil.copyfile(src, tmp)
        os.chmod(tmp, mode)
        os.replace(tmp, dest)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _ensure_dir(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True)
    current = stat.S_IMODE(path.stat().st_mode)
    if current != mode:
        path.chmod(mode)


def _atomic_write_text(dest: Path, text: str, mode: int) -> None:
    tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, mode)
        os.replace(tmp, dest)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _disable_headless_memories_config(text: str) -> str:
    lines = text.splitlines(keepends=True)
    out: list[str] = []
    in_features = False
    saw_features = False
    wrote_memories = False

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_features and not wrote_memories:
                out.append("memories = false\n")
                wrote_memories = True
            in_features = stripped == "[features]"
            saw_features = saw_features or in_features
        if in_features and stripped.startswith("memories"):
            out.append("memories = false\n")
            wrote_memories = True
            continue
        out.append(line)

    if in_features and not wrote_memories:
        out.append("memories = false\n")
    elif not saw_features:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out.append("\n[features]\nmemories = false\n")

    return "".join(out)


def _copy_config(src: Path, dest: Path) -> None:
    text = src.read_text(encoding="utf-8")
    _atomic_write_text(dest, _disable_headless_memories_config(text), 0o600)


def _copy_config_referenced_files(config: Path, home: Path, desktop_home: Path) -> list[str]:
    try:
        values = tomllib.loads(config.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError):
        return []  # Codex reports the invalid config itself.

    synced: list[str] = []
    for key in CONFIG_RELATIVE_FILE_KEYS:
        value = values.get(key)
        if not isinstance(value, str) or not value or value.startswith("~"):
            continue
        relative = Path(value)
        if relative.is_absolute() or ".." in relative.parts:
            continue
        src = desktop_home / relative
        dest = home / relative
        if not src.is_file():
            # Mirror the desktop home rather than keep a stale copy.
            dest.unlink(missing_ok=True)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        _atomic_copy(src, dest, 0o600)
        synced.append(value)
    return synced


def _require_nonempty_string(provider: dict[str, object], field: str) -> str:
    value = provider.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(
            f"restricted provider field {field!r} must be a non-empty string"
        )
    return value


def _render_restricted_provider_config(src: Path) -> str:
    try:
        config = tomllib.loads(src.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise RuntimeError("desktop Codex config is not readable valid TOML") from exc

    provider_id = config.get("model_provider")
    if not isinstance(provider_id, str) or not provider_id.strip():
        raise RuntimeError("restricted provider requires a selected model_provider")

    providers = config.get("model_providers")
    if not isinstance(providers, dict):
        raise MaterializationError("restricted provider requires a model_providers table")
    provider = providers.get(provider_id)
    if not isinstance(provider, dict):
        raise MaterializationError("selected restricted provider definition is missing")

    actual_fields = set(provider)
    required_fields = set(RESTRICTED_PROVIDER_REQUIRED_FIELDS)
    approved_fields = required_fields | set(RESTRICTED_PROVIDER_OPTIONAL_FIELDS)
    if not actual_fields.issuperset(required_fields) or not actual_fields.issubset(
        approved_fields
    ):
        missing = sorted(required_fields - actual_fields)
        extra = sorted(actual_fields - approved_fields)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if extra:
            details.append("extra=" + ",".join(extra))
        raise RuntimeError(
            "selected restricted provider fields do not match the approved schema"
            + (": " + " ".join(details) if details else "")
        )

    name = _require_nonempty_string(provider, "name")
    base_url = _require_nonempty_string(provider, "base_url")
    bearer_token = _require_nonempty_string(provider, "experimental_bearer_token")
    wire_api = _require_nonempty_string(provider, "wire_api")
    requires_openai_auth = provider.get("requires_openai_auth")
    supports_standalone_web_search = provider.get("supports_standalone_web_search")

    parsed_url = urlsplit(base_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        raise RuntimeError("restricted provider base_url must be an absolute HTTP URL")
    if wire_api != "responses":
        raise RuntimeError("restricted provider wire_api must be 'responses'")
    if not isinstance(requires_openai_auth, bool):
        raise MaterializationError(
            "restricted provider field 'requires_openai_auth' must be a boolean"
        )
    if (
        supports_standalone_web_search is not None
        and not isinstance(supports_standalone_web_search, bool)
    ):
        raise MaterializationError(
            "restricted provider field 'supports_standalone_web_search' "
            "must be a boolean"
        )

    def quote(value: str) -> str:
        return json.dumps(value, ensure_ascii=True)

    lines = [
        f"model_provider = {quote(provider_id)}",
        "",
        f"[model_providers.{quote(provider_id)}]",
        f"name = {quote(name)}",
        f"base_url = {quote(base_url)}",
        f"experimental_bearer_token = {quote(bearer_token)}",
        f"wire_api = {quote(wire_api)}",
        "requires_openai_auth = " + ("true" if requires_openai_auth else "false"),
    ]
    if supports_standalone_web_search is not None:
        lines.append(
            "supports_standalone_web_search = "
            + ("true" if supports_standalone_web_search else "false")
        )
    lines.append("")
    return "\n".join(lines)


def _validate_restricted_home(home: Path, desktop_home: Path) -> None:
    if home.resolve() == desktop_home.resolve():
        raise RuntimeError("restricted target home must differ from desktop Codex home")
    if not home.exists():
        return
    if home.is_symlink() or not home.is_dir():
        raise RuntimeError("restricted target home is not a directory")

    marker = home / RESTRICTED_HOME_MARKER
    entries = list(home.iterdir())
    if entries and (marker.is_symlink() or not marker.is_file()):
        raise RuntimeError(
            "restricted target home must be empty or already initialized for restricted use"
        )
    forbidden = sorted(
        entry.name
        for entry in entries
        if entry.name in RESTRICTED_FORBIDDEN_ENTRIES
        or entry.name.lower().lstrip(".").startswith("mcp")
    )
    if forbidden:
        raise RuntimeError(
            "restricted target home contains forbidden capability or credential entries: "
            + ",".join(forbidden)
        )
    unexpected = sorted(
        entry.name
        for entry in entries
        if (
            entry.name not in {RESTRICTED_HOME_MARKER, "config.toml", "installation_id"}
            and entry.name not in RESTRICTED_RUNTIME_DIRECTORIES
            and RESTRICTED_RUNTIME_FILE_RE.fullmatch(entry.name) is None
        )
    )
    if unexpected:
        raise RuntimeError(
            "restricted target home contains unexpected entries: "
            + ",".join(unexpected)
        )
    for entry in entries:
        if entry.is_symlink():
            raise RuntimeError("restricted target home entries must not be symlinks")
        if entry.name in RESTRICTED_RUNTIME_DIRECTORIES:
            if not entry.is_dir():
                raise RuntimeError("restricted runtime directory is not a directory")
            mode = stat.S_IMODE(entry.stat().st_mode)
            if mode not in RESTRICTED_RUNTIME_DIRECTORY_INPUT_MODES:
                raise RuntimeError("restricted runtime directory mode is unsafe")
            continue
        if not entry.is_file():
            raise RuntimeError("restricted target home entries must be regular files")


def _normalize_restricted_runtime_directories(home: Path) -> None:
    for name in sorted(RESTRICTED_RUNTIME_DIRECTORIES):
        directory = home / name
        if directory.exists():
            _ensure_dir(directory)


def _materialize_full(home: Path, desktop_home: Path) -> list[str]:
    if not desktop_home.is_dir():
        raise RuntimeError(f"desktop Codex home missing: {desktop_home}")
    if (home / RESTRICTED_HOME_MARKER).exists():
        raise RuntimeError("full materialization refuses a restricted target home")

    _ensure_dir(home)
    synced: list[str] = []

    for name in REQUIRED_FILES:
        src = desktop_home / name
        if not src.is_file():
            raise RuntimeError(f"required Codex file missing: {src}")
        if name == "config.toml":
            _copy_config(src, home / name)
        else:
            _atomic_copy(src, home / name, 0o600)
        synced.append(name)

    for name in OPTIONAL_FILES:
        src = desktop_home / name
        if not src.is_file():
            # Codex may authenticate through the selected provider or the OS
            # credential store. Mirror a removed file instead of requiring it
            # or retaining credentials from a previous desktop account.
            if name == "auth.json":
                (home / name).unlink(missing_ok=True)
            continue
        mode = 0o600 if name.endswith(".json") else 0o644
        _atomic_copy(src, home / name, mode)
        synced.append(name)

    synced += _copy_config_referenced_files(desktop_home / "config.toml", home, desktop_home)

    for subdir in ("sessions", "log", "tmp"):
        _ensure_dir(home / subdir)

    return synced


def _materialize_restricted_provider(home: Path, desktop_home: Path) -> list[str]:
    if not desktop_home.is_dir():
        raise RuntimeError(f"desktop Codex home missing: {desktop_home}")
    src = desktop_home / "config.toml"
    if not src.is_file():
        raise RuntimeError(f"required Codex file missing: {src}")

    restricted_config = _render_restricted_provider_config(src)
    _validate_restricted_home(home, desktop_home)
    _ensure_dir(home)
    _normalize_restricted_runtime_directories(home)
    _atomic_write_text(home / RESTRICTED_HOME_MARKER, "restricted-provider-v1\n", 0o600)
    _atomic_write_text(home / "config.toml", restricted_config, 0o600)
    return ["config.toml"]


def materialize(
    home: Path,
    desktop_home: Path,
    mode: str = MATERIALIZE_MODE_FULL,
) -> list[str]:
    if mode == MATERIALIZE_MODE_FULL:
        return _materialize_full(home, desktop_home)
    if mode == MATERIALIZE_MODE_RESTRICTED_PROVIDER:
        return _materialize_restricted_provider(home, desktop_home)
    raise ValueError(f"unsupported materialize mode: {mode}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True, help="target headless CODEX_HOME")
    parser.add_argument(
        "--desktop-home",
        default=str(Path.home() / ".codex"),
        help="source desktop Codex home",
    )
    parser.add_argument(
        "--mode",
        choices=MATERIALIZE_MODES,
        default=MATERIALIZE_MODE_FULL,
        help="materialization policy; restricted-provider emits provider-only config",
    )
    args = parser.parse_args(argv)

    try:
        synced = materialize(
            Path(args.home).expanduser(),
            Path(args.desktop_home).expanduser(),
            mode=args.mode,
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"codex_home_runtime: {exc}", file=sys.stderr)
        return 1

    print("codex_home_runtime: synced " + ",".join(synced))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
