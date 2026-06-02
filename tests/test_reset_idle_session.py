import importlib.util
import json
import os
import time
from pathlib import Path


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "reset_idle_session.py"
_SPEC = importlib.util.spec_from_file_location("reset_idle_session", _SCRIPT)
reset_idle_session = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(reset_idle_session)


def _write_state(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data))


def test_reset_if_idle_skips_recent_session(tmp_path):
    state_file = tmp_path / "session.json"
    _write_state(
        state_file,
        {
            "session_id": "sid-1",
            "engine_session_ids": {"claude": "sid-1"},
            "last_activity_at": 1000.0,
        },
    )

    changed, message = reset_idle_session.reset_if_idle(
        state_file,
        idle_seconds=24 * 60 * 60,
        now=1000.0 + 60,
    )

    assert changed is False
    assert message.startswith("recent activity")
    state = json.loads(state_file.read_text())
    assert state["session_id"] == "sid-1"
    assert state["engine_session_ids"]["claude"] == "sid-1"


def test_reset_if_idle_clears_stale_session_ids(tmp_path):
    state_file = tmp_path / "session.json"
    _write_state(
        state_file,
        {
            "session_id": "sid-1",
            "engine_session_ids": {"claude": "sid-1", "codex": "sid-2"},
            "last_activity_at": 1000.0,
            "recent_sids": ["sid-1"],
        },
    )

    changed, message = reset_idle_session.reset_if_idle(
        state_file,
        idle_seconds=24 * 60 * 60,
        now=1000.0 + 25 * 60 * 60,
    )

    assert changed is True
    assert message.startswith("reset:")
    state = json.loads(state_file.read_text())
    assert state["session_id"] == ""
    assert state["engine_session_ids"] == {"claude": "", "codex": ""}
    assert state["recent_sids"] == ["sid-1"]
    assert state["last_activity_at"] == 1000.0


def test_reset_if_idle_falls_back_to_state_file_mtime(tmp_path):
    state_file = tmp_path / "session.json"
    _write_state(state_file, {"session_id": "sid-1"})
    mtime = 1000
    state_file.touch()
    os.utime(state_file, (mtime, mtime))

    changed, message = reset_idle_session.reset_if_idle(
        state_file,
        idle_seconds=24 * 60 * 60,
        now=mtime + 25 * 60 * 60,
    )

    assert changed is True
    assert message.startswith("reset:")
    assert json.loads(state_file.read_text())["session_id"] == ""


def test_reset_if_idle_treats_recent_pending_wx_batch_as_activity(tmp_path):
    state_file = tmp_path / "session.json"
    pending_file = tmp_path / "pending.json"
    _write_state(
        state_file,
        {
            "session_id": "sid-1",
            "engine_session_ids": {"claude": "sid-1"},
            "last_activity_at": 1000.0,
        },
    )
    _write_state(
        pending_file,
        {
            "pending": {
                "record-1": {
                    "received_at": 1000.0 + 25 * 60 * 60 - 60,
                    "units": [],
                }
            }
        },
    )

    changed, message = reset_idle_session.reset_if_idle(
        state_file,
        idle_seconds=24 * 60 * 60,
        pending_file=pending_file,
        now=1000.0 + 25 * 60 * 60,
    )

    assert changed is False
    assert message.startswith("recent pending activity")
    state = json.loads(state_file.read_text())
    assert state["session_id"] == "sid-1"
    assert state["engine_session_ids"]["claude"] == "sid-1"


def test_main_returns_zero_for_recent_pending_wx_batch(tmp_path, capsys):
    state_file = tmp_path / "session.json"
    pending_file = tmp_path / "pending.json"
    time_marker = time.time()
    _write_state(
        state_file,
        {
            "session_id": "sid-1",
            "engine_session_ids": {"claude": "sid-1"},
            "last_activity_at": time_marker - 25 * 60 * 60,
        },
    )
    _write_state(
        pending_file,
        {
            "pending": {
                "record-1": {
                    "received_at": time_marker - 60,
                    "units": [],
                }
            }
        },
    )

    rc = reset_idle_session.main(
        [
            str(state_file),
            "--pending-file",
            str(pending_file),
            "--idle-minutes",
            "180",
        ]
    )

    assert rc == 0
    assert "recent pending activity" in capsys.readouterr().out


def test_default_idle_minutes_is_three_hours(monkeypatch):
    monkeypatch.delenv("BABATA_WX_DAILY_RESET_IDLE_MINUTES", raising=False)

    assert reset_idle_session._default_idle_minutes() == 180


def test_reset_if_idle_allows_stale_pending_wx_batch(tmp_path):
    state_file = tmp_path / "session.json"
    pending_file = tmp_path / "pending.json"
    _write_state(
        state_file,
        {
            "session_id": "sid-1",
            "engine_session_ids": {"claude": "sid-1"},
            "last_activity_at": 1000.0,
        },
    )
    _write_state(
        pending_file,
        {
            "pending": {
                "record-1": {
                    "received_at": 1000.0 + 60,
                    "units": [],
                }
            }
        },
    )

    changed, message = reset_idle_session.reset_if_idle(
        state_file,
        idle_seconds=24 * 60 * 60,
        pending_file=pending_file,
        now=1000.0 + 25 * 60 * 60,
    )

    assert changed is True
    assert message.startswith("reset:")
    assert json.loads(state_file.read_text())["session_id"] == ""
