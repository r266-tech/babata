#!/usr/bin/env python3
"""Clear a babata session state file only when it has been idle long enough."""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path
from typing import Any


def _active_session_ids(state: dict[str, Any]) -> list[str]:
    active: list[str] = []
    sid = state.get("session_id")
    if isinstance(sid, str) and sid:
        active.append(sid)

    engine_sids = state.get("engine_session_ids")
    if isinstance(engine_sids, dict):
        for value in engine_sids.values():
            if isinstance(value, str) and value:
                active.append(value)
    return active


def _latest_pending_activity(path: Path) -> tuple[float | None, str | None]:
    """Return newest pending received_at, or a message when pending is unusable."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None, None
    except Exception as exc:
        return None, f"pending file unreadable: {type(exc).__name__}: {exc}"
    if not isinstance(data, dict):
        return None, "pending file is not a JSON object"
    pending = data.get("pending")
    if not isinstance(pending, dict) or not pending:
        return None, None

    latest: float | None = None
    missing_ts = 0
    for record in pending.values():
        if not isinstance(record, dict):
            continue
        received_at = record.get("received_at")
        if isinstance(received_at, (int, float)):
            latest = max(latest or float(received_at), float(received_at))
        else:
            missing_ts += 1
    if latest is None:
        return None, f"pending activity without timestamp: count={len(pending)}"
    if missing_ts:
        return latest, f"pending activity missing timestamp: count={missing_ts}"
    return latest, None


def _atomic_write_json(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def reset_if_idle(
    path: Path,
    *,
    idle_seconds: int,
    pending_file: Path | None = None,
    now: float | None = None,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Return (changed, message). A reset clears active sid fields only."""
    if idle_seconds < 0:
        raise ValueError("idle_seconds must be >= 0")

    try:
        state = json.loads(path.read_text())
    except FileNotFoundError:
        return False, f"state file missing: {path}"
    except Exception as exc:
        return False, f"state file unreadable: {type(exc).__name__}: {exc}"
    if not isinstance(state, dict):
        return False, "state file is not a JSON object"

    active = _active_session_ids(state)
    if not active:
        return False, "no active session"

    last = state.get("last_activity_at")
    if not isinstance(last, (int, float)):
        try:
            last = path.stat().st_mtime
        except OSError as exc:
            return False, f"no usable activity timestamp: {exc}"

    pending_note = None
    pending_used = False
    if pending_file is not None:
        pending_last, pending_note = _latest_pending_activity(pending_file)
        if pending_last is not None:
            last = max(float(last), pending_last)
            pending_used = True

    now = time.time() if now is None else now
    elapsed = now - float(last)
    if elapsed < 0:
        return False, f"activity timestamp is in the future by {-elapsed:.0f}s"
    if elapsed <= idle_seconds:
        prefix = "recent pending activity" if pending_used else "recent activity"
        return False, f"{prefix}: elapsed={elapsed:.0f}s threshold={idle_seconds}s"
    if pending_note:
        return False, pending_note

    if dry_run:
        return True, f"would reset: elapsed={elapsed:.0f}s threshold={idle_seconds}s"

    state["session_id"] = ""
    engine_sids = state.get("engine_session_ids")
    if isinstance(engine_sids, dict):
        for key in list(engine_sids.keys()):
            engine_sids[key] = ""
    _atomic_write_json(path, state)
    return True, f"reset: elapsed={elapsed:.0f}s threshold={idle_seconds}s"


def _default_idle_minutes() -> int:
    raw = os.environ.get("BABATA_WX_DAILY_RESET_IDLE_MINUTES", "180")
    try:
        return max(0, int(raw))
    except ValueError:
        return 180


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("state_file", type=Path)
    parser.add_argument("--idle-minutes", type=int, default=_default_idle_minutes())
    parser.add_argument("--pending-file", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    changed, message = reset_if_idle(
        args.state_file,
        idle_seconds=max(0, args.idle_minutes) * 60,
        pending_file=args.pending_file,
        dry_run=args.dry_run,
    )
    print(message)
    ok_prefixes = (
        "recent activity",
        "recent pending activity",
        "no active session",
        "state file missing",
    )
    return 0 if changed or message.startswith(ok_prefixes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
