#!/usr/bin/env python3
"""Move legacy babata data roots into PROJECT_STATE_DIR.

The script is intentionally conservative:
- default mode is --dry-run;
- it copies legacy data into the new target before touching the old path;
- on apply, it moves the old path into STATE_DIR/backups/data-root-migration/
  and leaves a symlink at the legacy path for old code/process compatibility;
- it copies the backup into the target a second time to catch writes that
  happened just before the move.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

load_dotenv(REPO_ROOT / ".env", override=False)

from constants import STATE_DIR  # noqa: E402


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return a.absolute() == b.absolute()


def _copy_merge(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    if src.is_symlink():
        return
    if src.is_file():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return
    for item in src.rglob("*"):
        rel = item.relative_to(src)
        target = dst / rel
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _migrate_one(name: str, legacy: Path, target: Path, backup_root: Path, apply: bool) -> None:
    print(f"{name}:")
    print(f"  legacy: {legacy}")
    print(f"  target: {target}")

    if _same_path(legacy, target):
        print("  status: already same path")
        return
    if legacy.is_symlink() and _same_path(legacy, target):
        print("  status: legacy symlink already points at target")
        return
    if not legacy.exists():
        print("  status: no legacy path")
        if apply and target.exists():
            legacy.parent.mkdir(parents=True, exist_ok=True)
            legacy.symlink_to(target)
            print("  action: created compatibility symlink")
        return

    if target.exists() and target.is_file():
        raise SystemExit(f"target is a file, refusing: {target}")

    if not apply:
        print("  action: would copy legacy into target, move legacy to backup, then symlink legacy -> target")
        return

    target.mkdir(parents=True, exist_ok=True)
    _copy_merge(legacy, target)

    backup = backup_root / name
    backup.parent.mkdir(parents=True, exist_ok=True)
    if backup.exists():
        raise SystemExit(f"backup already exists, refusing: {backup}")
    shutil.move(str(legacy), str(backup))
    legacy.symlink_to(target)
    _copy_merge(backup, target)
    print(f"  action: migrated; legacy backup at {backup}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="perform the migration; default is dry-run")
    args = parser.parse_args(argv)

    backup_root = STATE_DIR / "backups" / "data-root-migration" / time.strftime("%Y%m%d-%H%M%S")
    sidebar_target = (
        Path(os.environ["BABATA_SIDEBAR_DATA_DIR"]).expanduser()
        if os.environ.get("BABATA_SIDEBAR_DATA_DIR")
        else STATE_DIR / "sidebar"
    )
    weixin_target = (
        Path(os.environ["BABATA_WEIXIN_DIR"]).expanduser()
        if os.environ.get("BABATA_WEIXIN_DIR")
        else STATE_DIR / "weixin"
    )
    jobs = [
        ("sidebar", Path.home() / ".babata" / "sidebar", sidebar_target),
        ("weixin", Path.home() / ".babata" / "weixin", weixin_target),
    ]

    print(f"mode: {'apply' if args.apply else 'dry-run'}")
    print(f"state_dir: {STATE_DIR}")
    print(f"backup_root: {backup_root}")
    for job in jobs:
        _migrate_one(*job, backup_root=backup_root, apply=args.apply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
