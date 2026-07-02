import asyncio
import json
from pathlib import Path

import cc as cc_module
import sidebar_bot


def test_cc_exposes_public_session_helpers(tmp_path):
    state_file = tmp_path / "session.json"
    state_file.write_text('{"session_id": "sid-existing", "recent_sids": ["sid-existing", 42, ""]}')

    session = cc_module.CC(state_file=state_file, source_prompt="Source: test.")

    assert session.session_id == "sid-existing"
    assert session.assistant_engine_name is None
    assert session.recent_session_ids() == ["sid-existing"]
    session.persist_current_session()
    state = json.loads(state_file.read_text())
    assert state["session_id"] == "sid-existing"
    assert state["recent_sids"] == ["sid-existing"]
    assert "last_activity_at" in state


def test_sidebar_cpu_status_reads_public_session_property(monkeypatch):
    class FakeLock:
        def __init__(self, locked: bool):
            self._locked = locked

        def locked(self):
            return self._locked

    class FakeEngine:
        def __init__(self, name: str = "codex", sid: str | None = None):
            self._name = name
            self._sid = sid

        @property
        def session_id(self):
            return self._sid

        @property
        def assistant_engine_name(self):
            return self._name

    def engine_name_for(obj, _state_file: Path) -> str:
        return obj._name

    monkeypatch.setattr(sidebar_bot, "_engine_name_for", engine_name_for)
    monkeypatch.setattr(sidebar_bot, "_cc_lock", FakeLock(False))
    monkeypatch.setattr(sidebar_bot, "_proactive_lock", FakeLock(True))
    monkeypatch.setattr(sidebar_bot, "cc", FakeEngine("codex", "sid-visible"))
    monkeypatch.setattr(sidebar_bot, "proactive_cc", FakeEngine("codex"))

    payload = sidebar_bot._cpu_status_payload()

    assert payload["session_id"] == "sid-visible"
    assert payload["busy"] is False
    assert payload["chat_busy"] is False
    assert payload["proactive_busy"] is True


def test_sidebar_cpu_switch_persists_sessions_with_public_api(monkeypatch, tmp_path):
    class FakeLock:
        def __init__(self, locked: bool):
            self._locked = locked

        def locked(self):
            return self._locked

    class FakeEngine:
        def __init__(self, name: str = "codex"):
            self._name = name
            self.persisted = 0

        @property
        def session_id(self):
            return None

        @property
        def assistant_engine_name(self):
            return self._name

        def persist_current_session(self):
            self.persisted += 1

    def engine_name_for(obj, _state_file: Path) -> str:
        return obj._name

    current_chat = FakeEngine()
    current_proactive = FakeEngine()
    made: list[FakeEngine] = []

    def make_engine(target: str) -> FakeEngine:
        engine = FakeEngine(target)
        made.append(engine)
        return engine

    monkeypatch.setattr(sidebar_bot, "_engine_name_for", engine_name_for)
    monkeypatch.setattr(sidebar_bot, "_cc_lock", FakeLock(False))
    monkeypatch.setattr(sidebar_bot, "_proactive_lock", FakeLock(False))
    monkeypatch.setattr(sidebar_bot, "cc", current_chat)
    monkeypatch.setattr(sidebar_bot, "proactive_cc", current_proactive)
    monkeypatch.setattr(sidebar_bot, "_SIDEBAR_SESSION_FILE", tmp_path / "sidebar.json")
    monkeypatch.setattr(sidebar_bot, "_PROACTIVE_SESSION_FILE", tmp_path / "proactive.json")
    monkeypatch.setattr(sidebar_bot, "_make_sidebar_engine", make_engine)
    monkeypatch.setattr(sidebar_bot, "_make_proactive_engine", make_engine)

    payload = asyncio.run(sidebar_bot._switch_sidebar_cpu("claude"))

    assert payload["changed"] is True
    assert payload["cpu"] == "claude"
    assert current_chat.persisted == 1
    assert current_proactive.persisted == 1
    assert [engine.persisted for engine in made] == [1, 1]


def test_sidebar_cpu_switch_only_waits_for_chat_turn(monkeypatch, tmp_path):
    class FakeLock:
        def __init__(self, locked: bool):
            self._locked = locked

        def locked(self):
            return self._locked

    class FakeEngine:
        def __init__(self, name: str = "claude"):
            self._name = name

        @property
        def session_id(self):
            return None

        @property
        def assistant_engine_name(self):
            return self._name

        def persist_current_session(self):
            return None

    def engine_name_for(obj, _state_file: Path) -> str:
        return obj._name

    monkeypatch.setattr(sidebar_bot, "_engine_name_for", engine_name_for)
    monkeypatch.setattr(sidebar_bot, "cc", FakeEngine())
    monkeypatch.setattr(sidebar_bot, "proactive_cc", FakeEngine())
    monkeypatch.setattr(sidebar_bot, "_SIDEBAR_SESSION_FILE", tmp_path / "sidebar.json")
    monkeypatch.setattr(sidebar_bot, "_PROACTIVE_SESSION_FILE", tmp_path / "proactive.json")
    monkeypatch.setattr(sidebar_bot, "_make_sidebar_engine", lambda target: FakeEngine(target))
    monkeypatch.setattr(sidebar_bot, "_make_proactive_engine", lambda target: FakeEngine(target))

    monkeypatch.setattr(sidebar_bot, "_cc_lock", FakeLock(False))
    monkeypatch.setattr(sidebar_bot, "_proactive_lock", FakeLock(True))
    payload = asyncio.run(sidebar_bot._switch_sidebar_cpu("codex"))
    assert payload["changed"] is True

    monkeypatch.setattr(sidebar_bot, "cc", FakeEngine("claude"))
    monkeypatch.setattr(sidebar_bot, "proactive_cc", FakeEngine("claude"))
    monkeypatch.setattr(sidebar_bot, "_cc_lock", FakeLock(True))
    monkeypatch.setattr(sidebar_bot, "_proactive_lock", FakeLock(False))
    try:
        asyncio.run(sidebar_bot._switch_sidebar_cpu("codex"))
    except RuntimeError as exc:
        assert "sidebar turn" in str(exc)
    else:
        raise AssertionError("CPU switch should wait for active chat turns")


def test_sidebar_cpu_switch_does_not_reach_into_engine_private_session_state():
    source = Path(sidebar_bot.__file__).read_text()

    assert 'getattr(cc, "_session_id"' not in source
    assert 'getattr(obj, "_babata_engine_name"' not in source
    assert "._record_sid(" not in source


def test_sidebar_proactive_prompt_stays_thin_and_boundary_focused():
    prompt = sidebar_bot._PROACTIVE_PROMPT

    assert len(prompt) <= 420
    assert "默认静默" in prompt
    assert "mascot_speak" in prompt
    assert "suggest_prompts" in prompt
    assert "tab_id/window_id" in prompt
    assert "不编造观察" in prompt
