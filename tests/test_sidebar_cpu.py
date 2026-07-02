import asyncio
from pathlib import Path

import cc as cc_module
import sidebar_bot


def test_cc_exposes_session_id_for_status_payloads(tmp_path):
    state_file = tmp_path / "session.json"
    state_file.write_text('{"session_id": "sid-existing"}')

    session = cc_module.CC(state_file=state_file, source_prompt="Source: test.")

    assert session.session_id == "sid-existing"


def test_sidebar_cpu_status_distinguishes_chat_and_proactive_busy(monkeypatch):
    class FakeLock:
        def __init__(self, locked: bool):
            self._locked = locked

        def locked(self):
            return self._locked

    monkeypatch.setattr(sidebar_bot, "_cc_lock", FakeLock(False))
    monkeypatch.setattr(sidebar_bot, "_proactive_lock", FakeLock(True))

    payload = sidebar_bot._cpu_status_payload()

    assert payload["busy"] is False
    assert payload["chat_busy"] is False
    assert payload["proactive_busy"] is True


def test_sidebar_cpu_switch_only_blocks_on_chat_turn(monkeypatch, tmp_path):
    class FakeLock:
        def __init__(self, locked: bool):
            self._locked = locked

        def locked(self):
            return self._locked

    class FakeEngine:
        def __init__(self, name: str = "codex"):
            self._name = name
            self.recorded: list[str | None] = []

        @property
        def session_id(self):
            return None

        def _record_sid(self, sid: str | None):
            self.recorded.append(sid)

    def engine_name_for(obj, _state_file: Path) -> str:
        return obj._name

    monkeypatch.setattr(sidebar_bot, "_engine_name_for", engine_name_for)
    monkeypatch.setattr(sidebar_bot, "_cc_lock", FakeLock(False))
    monkeypatch.setattr(sidebar_bot, "_proactive_lock", FakeLock(True))
    monkeypatch.setattr(sidebar_bot, "cc", FakeEngine())
    monkeypatch.setattr(sidebar_bot, "proactive_cc", FakeEngine())
    monkeypatch.setattr(sidebar_bot, "_SIDEBAR_SESSION_FILE", tmp_path / "sidebar.json")
    monkeypatch.setattr(sidebar_bot, "_PROACTIVE_SESSION_FILE", tmp_path / "proactive.json")
    monkeypatch.setattr(sidebar_bot, "_make_sidebar_engine", lambda target: FakeEngine(target))
    monkeypatch.setattr(sidebar_bot, "_make_proactive_engine", lambda target: FakeEngine(target))

    payload = asyncio.run(sidebar_bot._switch_sidebar_cpu("claude"))

    assert payload["changed"] is True
    assert payload["cpu"] == "claude"

    monkeypatch.setattr(sidebar_bot, "cc", FakeEngine("claude"))
    monkeypatch.setattr(sidebar_bot, "proactive_cc", FakeEngine("claude"))
    monkeypatch.setattr(sidebar_bot, "_cc_lock", FakeLock(True))
    monkeypatch.setattr(sidebar_bot, "_proactive_lock", FakeLock(False))
    try:
        asyncio.run(sidebar_bot._switch_sidebar_cpu("codex"))
    except RuntimeError as exc:
        assert "sidebar turn" in str(exc)
    else:
        raise AssertionError("CPU switch should wait for chat turn to finish")
