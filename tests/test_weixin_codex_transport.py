import pytest

from codex_engine import CodexEngine


def build_command(tmp_path, source="wechat", sid=None):
    session = CodexEngine(
        state_file=tmp_path / "session.json",
        source_prompt="Source: test.",
        memory_source=source,
        memory_enabled=False,
    )
    session._session_id = sid
    return session._build_command("hello", [], tmp_path / "last.txt")[0]


@pytest.mark.parametrize("sid", [None, "existing-session"])
def test_weixin_http_transport_preserves_provider_and_resume(monkeypatch, tmp_path, sid):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("BABATA_WEIXIN_CODEX_WEBSOCKETS", "0")
    monkeypatch.delenv("BABATA_CODEX_IGNORE_USER_CONFIG", raising=False)
    (tmp_path / "config.toml").write_text('model_provider = "custom-provider"\n')

    cmd = build_command(tmp_path, sid=sid)

    assert "model_providers.custom-provider.supports_websockets=false" in cmd
    assert not any(arg.startswith("model_provider=") for arg in cmd)
    if sid:
        assert "resume" in cmd
        assert sid in cmd


@pytest.mark.parametrize("source,setting", [("telegram", "0"), ("wechat", "1"), ("wechat", "")])
def test_weixin_http_option_preserves_other_transports(monkeypatch, tmp_path, source, setting):
    monkeypatch.setenv("BABATA_WEIXIN_CODEX_WEBSOCKETS", setting)

    assert not any("supports_websockets" in arg for arg in build_command(tmp_path, source))


def test_weixin_http_with_ignored_config_uses_builtin_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setenv("BABATA_WEIXIN_CODEX_WEBSOCKETS", "0")
    monkeypatch.setenv("BABATA_CODEX_IGNORE_USER_CONFIG", "1")
    (tmp_path / "config.toml").write_text("invalid config!")

    assert "model_providers.openai.supports_websockets=false" in build_command(tmp_path)
