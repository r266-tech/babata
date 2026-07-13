#!/usr/bin/env python3
"""Materialize a scoped Codex home for headless Babata CPU runs.

The desktop Codex app owns the live auth/config state under ~/.codex. Cron jobs
run with a separate CODEX_HOME so they do not write into the interactive app
home. This helper copies only the small files needed for `codex exec` to start.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path


REQUIRED_FILES = ("auth.json", "config.toml")
OPTIONAL_FILES = (
    "provider-slots.json",
    "models_cache.json",
    "version.json",
)


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
    tmp = dest.with_name(f".{dest.name}.tmp-{os.getpid()}")
    try:
        tmp.write_text(_disable_headless_memories_config(text), encoding="utf-8")
        os.chmod(tmp, 0o644)
        os.replace(tmp, dest)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def materialize(home: Path, desktop_home: Path) -> list[str]:
    if not desktop_home.is_dir():
        raise RuntimeError(f"desktop Codex home missing: {desktop_home}")

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
            continue
        mode = 0o600 if name.endswith(".json") else 0o644
        _atomic_copy(src, home / name, mode)
        synced.append(name)

    for subdir in ("sessions", "log", "tmp"):
        _ensure_dir(home / subdir)

    return synced


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True, help="target headless CODEX_HOME")
    parser.add_argument(
        "--desktop-home",
        default=str(Path.home() / ".codex"),
        help="source desktop Codex home",
    )
    args = parser.parse_args(argv)

    try:
        synced = materialize(Path(args.home).expanduser(), Path(args.desktop_home).expanduser())
    except Exception as exc:
        print(f"codex_home_runtime: {exc}", file=sys.stderr)
        return 1

    print("codex_home_runtime: synced " + ",".join(synced))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
