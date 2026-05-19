from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import time
from pathlib import Path

import pytest


def load_tool(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_PROVIDER_HOME", str(tmp_path))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    tool_path = Path(__file__).resolve().parents[1] / "codex-provider"
    loader = importlib.machinery.SourceFileLoader("codex_provider_macos", str(tool_path))
    spec = importlib.util.spec_from_loader("codex_provider_macos", loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_codex_home(tool):
    tool.CODEX_HOME.mkdir(parents=True)
    tool.APP_PROFILE.mkdir(parents=True)
    (tool.APP_PROFILE / "Local State").write_text("{}")
    tool.AUTH_JSON.write_text(json.dumps({"tokens": {"id_token": "bad.token.value"}}))
    tool.CONFIG_TOML.write_text(
        'personality = "pragmatic"\n'
        'model = "old"\n'
        'model_provider = "openai"\n'
        'api_base_url = "https://api.openai.com/v1"\n'
        '\n'
        '[projects."/Users/friend/work"]\n'
        'trust_level = "trusted"\n'
    )


def seed_sessions(tool):
    with sqlite3.connect(tool.STATE_DB) as conn:
        conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT NOT NULL, rollout_path TEXT)")
        conn.execute("INSERT INTO threads VALUES ('t1', 'openai', '')")
        conn.execute("INSERT INTO threads VALUES ('t2', 'OpenAI', '')")
    rollout = tool.CODEX_HOME / "sessions" / "2026" / "05" / "18" / "rollout-test.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(
        '{"type":"session_meta","payload":{"id":"t1","model_provider":"openai"}}\n'
        '{"type":"event_msg","timestamp":"2026-05-18T00:00:00Z"}\n'
    )
    old_ns = time.time_ns() - 10_000_000_000
    os.utime(rollout, ns=(old_ns, old_ns))
    return rollout, old_ns


def test_relay_switch_preserves_config_projects_and_aggregates_sessions(monkeypatch, tmp_path):
    tool = load_tool(monkeypatch, tmp_path)
    seed_codex_home(tool)
    rollout, old_ns = seed_sessions(tool)
    calls = []
    monkeypatch.setattr(tool, "quit_codex", lambda: calls.append("quit") or True)
    monkeypatch.setattr(tool, "launch_codex", lambda: calls.append("launch"))
    monkeypatch.setattr(tool, "probe_relay", lambda base_url, api_key: (True, "ok"))

    rc = tool.cmd_relay("https://relay.example.com/v1", "test-api-key", slot=None)

    assert rc == 0
    assert calls[-1] == "launch"
    assert calls.count("launch") == 1
    assert calls.count("quit") >= 1
    data = json.loads(tool.SLOTS_FILE.read_text())
    assert data["current"] == "relay"
    assert data["app_history_slot"] == "local"
    assert os.readlink(tool.AUTH_JSON) == str(tool.CODEX_HOME / "auth.relay.json")
    assert os.readlink(tool.APP_PROFILE) == str(tool.APP_SUPPORT / "Codex.local")
    assert json.loads((tool.CODEX_HOME / "auth.relay.json").read_text()) == {
        "OPENAI_API_KEY": "test-api-key",
        "auth_mode": "apikey",
    }

    config = tool.CONFIG_TOML.read_text()
    assert 'model = "gpt-5.5"' in config
    assert 'model_provider = "OpenAI"' in config
    assert 'base_url = "https://relay.example.com"' in config
    assert '[projects."/Users/friend/work"]' in config
    assert 'trust_level = "trusted"' in config

    with sqlite3.connect(tool.STATE_DB) as conn:
        rows = conn.execute("SELECT model_provider, COUNT(*) FROM threads GROUP BY model_provider").fetchall()
    assert rows == [("OpenAI", 2)]
    assert '"model_provider":"OpenAI"' in rollout.read_text()
    assert rollout.stat().st_mtime_ns == old_ns


def test_clean_current_slot_is_noop(monkeypatch, tmp_path):
    tool = load_tool(monkeypatch, tmp_path)
    seed_codex_home(tool)
    seed_sessions(tool)
    monkeypatch.setattr(tool, "quit_codex", lambda: True)
    monkeypatch.setattr(tool, "launch_codex", lambda: None)
    monkeypatch.setattr(tool, "probe_relay", lambda base_url, api_key: (True, "ok"))
    assert tool.cmd_relay("https://relay.example.com", "test-api-key", slot="relay") == 0

    calls = []
    monkeypatch.setattr(tool, "quit_codex", lambda: calls.append("quit") or True)
    monkeypatch.setattr(tool, "launch_codex", lambda: calls.append("launch"))

    assert tool.cmd_switch("relay") == 0
    assert calls == []


def test_oauth_switch_restores_builtin_provider_without_dropping_projects(monkeypatch, tmp_path):
    tool = load_tool(monkeypatch, tmp_path)
    seed_codex_home(tool)
    seed_sessions(tool)
    monkeypatch.setattr(tool, "quit_codex", lambda: True)
    monkeypatch.setattr(tool, "launch_codex", lambda: None)
    monkeypatch.setattr(tool, "probe_relay", lambda base_url, api_key: (True, "ok"))
    assert tool.cmd_relay("https://relay.example.com", "test-api-key", slot="relay") == 0

    data = json.loads(tool.SLOTS_FILE.read_text())
    data["slots"]["personal"] = {
        "display_name": "Codex - personal",
        "mode": "oauth",
        "auth_file": str(tool.CODEX_HOME / "auth.personal.json"),
        "app_profile_dir": str(tool.APP_SUPPORT / "Codex.personal"),
    }
    (tool.CODEX_HOME / "auth.personal.json").write_text('{"tokens": {"id_token": "x.y.z"}}')
    tool.save_slots(data)

    assert tool.cmd_switch("personal") == 0
    config = tool.CONFIG_TOML.read_text()
    assert 'model_provider = "openai"' in config
    assert "[model_providers.OpenAI]" not in config
    assert '[projects."/Users/friend/work"]' in config
    assert os.readlink(tool.APP_PROFILE) == str(tool.APP_SUPPORT / "Codex.local")


def test_oauth_command_registers_slot_and_prompts_for_login(monkeypatch, tmp_path):
    tool = load_tool(monkeypatch, tmp_path)
    seed_codex_home(tool)
    seed_sessions(tool)
    calls = []
    monkeypatch.setattr(tool, "quit_codex", lambda: calls.append("quit") or True)
    monkeypatch.setattr(tool, "launch_codex", lambda: calls.append("launch"))

    assert tool.cmd_oauth("work") == 0

    data = json.loads(tool.SLOTS_FILE.read_text())
    assert data["current"] == "work"
    assert data["slots"]["work"]["mode"] == "oauth"
    assert os.readlink(tool.AUTH_JSON) == str(tool.CODEX_HOME / "auth.work.json")
    assert os.readlink(tool.APP_PROFILE) == str(tool.APP_SUPPORT / "Codex.local")
    config = tool.CONFIG_TOML.read_text()
    assert 'model_provider = "openai"' in config
    assert "[model_providers.OpenAI]" not in config
    assert calls[-1] == "launch"


def test_menu_command_dispatches_interactive_menu(monkeypatch, tmp_path):
    tool = load_tool(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(tool, "cmd_menu", lambda: calls.append("menu") or 0)

    assert tool.main(["menu"]) == 0
    assert calls == ["menu"]


def test_no_args_non_tty_keeps_status_behavior(monkeypatch, tmp_path):
    tool = load_tool(monkeypatch, tmp_path)
    calls = []
    monkeypatch.setattr(tool.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(tool.sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(tool, "cmd_status", lambda: calls.append("status") or 0)

    assert tool.main([]) == 0
    assert calls == ["status"]


def test_version_and_more_commands(monkeypatch, tmp_path, capsys):
    tool = load_tool(monkeypatch, tmp_path)

    assert tool.main(["version"]) == 0
    version_out = capsys.readouterr().out
    assert "codex-provider" in version_out
    assert "github.com" in version_out

    assert tool.main(["more"]) == 0
    more_out = capsys.readouterr().out
    assert "Codex Provider 路径" in more_out
    assert str(tool.CODEX_HOME) in more_out


def test_public_help_stays_simple(monkeypatch, tmp_path, capsys):
    tool = load_tool(monkeypatch, tmp_path)

    with pytest.raises(SystemExit) as exc:
        tool.main(["--help"])

    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "在已保存的 Codex 账号和 API 渠道之间切换" in out
    assert "显示帮助并退出" in out
    assert "add-api" in out
    assert "add-account" in out
    assert "use" in out
    assert "menu" not in out
    assert "version" not in out
    assert "normalize-sessions" not in out
    assert "==SUPPRESS==" not in out
