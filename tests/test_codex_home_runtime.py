import stat

import pytest

import codex_home_runtime


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_materialize_copies_required_and_optional_files_with_safe_modes(tmp_path):
    desktop_home = tmp_path / "desktop"
    headless_home = tmp_path / "headless"
    desktop_home.mkdir()

    (desktop_home / "auth.json").write_text('{"token":"secret"}')
    (desktop_home / "config.toml").write_text('model = "gpt-5.5"\n')
    (desktop_home / "provider-slots.json").write_text("{}")
    (desktop_home / "AGENTS.md").write_text("agent instructions")

    synced = codex_home_runtime.materialize(headless_home, desktop_home)

    assert synced == ["auth.json", "config.toml", "provider-slots.json"]
    assert (headless_home / "auth.json").read_text() == '{"token":"secret"}'
    assert (headless_home / "config.toml").read_text() == 'model = "gpt-5.5"\n'
    assert _mode(headless_home) == 0o700
    assert _mode(headless_home / "auth.json") == 0o600
    assert _mode(headless_home / "config.toml") == 0o644
    assert _mode(headless_home / "provider-slots.json") == 0o600
    assert not (headless_home / "AGENTS.md").exists()
    for name in ("sessions", "log", "tmp"):
        assert (headless_home / name).is_dir()
        assert _mode(headless_home / name) == 0o700


def test_materialize_does_not_copy_desktop_prompt_adapter():
    assert "AGENTS.md" not in codex_home_runtime.OPTIONAL_FILES


def test_materialize_fails_when_required_file_is_missing(tmp_path):
    desktop_home = tmp_path / "desktop"
    desktop_home.mkdir()
    (desktop_home / "auth.json").write_text("{}")

    with pytest.raises(RuntimeError, match="required Codex file missing"):
        codex_home_runtime.materialize(tmp_path / "headless", desktop_home)
