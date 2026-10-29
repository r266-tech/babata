"""Permission policy at CPU and channel startup boundaries."""

import ast
import os
from pathlib import Path
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("trusted", [False, True])
def test_all_cpu_adapters_apply_policy_to_new_live_and_resumed_sessions(tmp_path, trusted):
    # Use the installed SDK in a fresh interpreter: cc's environment settings
    # are intentionally read at process startup, and unit-test SDK stubs must
    # not hide a rejected option or changed default.
    env = os.environ.copy()
    for key in [
        "BABATA_FULL_TRUST", "BABATA_CODEX_SANDBOX", "BABATA_GROK_PERMISSION_MODE",
        "BABATA_GROK_ALWAYS_APPROVE", "BABATA_SHARED_CC",
    ]:
        env.pop(key, None)
    env.update({
        "BABATA_FULL_TRUST": "1" if trusted else "0",
        "PROJECT_STATE_DIR": str(tmp_path / "state"),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    code = '''
import os
from pathlib import Path
from engine import make_engine
trusted = os.environ["BABATA_FULL_TRUST"] == "1"
for source in ["tg", "wechat"]:
    for name in ["codex", "claude", "grok"]:
        for live in [False, True]:
            session = make_engine(
                state_file=Path(os.environ["PROJECT_STATE_DIR"]) / "session.json",
                source_prompt="Permission regression test.", memory_source=source,
                memory_enabled=False, engine=name, live=live,
            )
            for sid in [None, "existing-session"]:
                session._session_id = sid
                if name == "codex":
                    cmd, _, _ = session._build_command("test", [], Path("/tmp/unused-last.txt"))
                    mode = "danger-full-access" if trusted else "workspace-write"
                    assert f\'sandbox_mode="{mode}"\' in cmd, cmd
                    assert \'approval_policy="never"\' in cmd
                    assert cmd[cmd.index("-C") + 1] == str(Path.cwd())
                    assert ("resume" in cmd) == bool(sid)
                elif name == "claude":
                    opts = session._make_options() if live else session._make_query_options(include_partial_messages=False)
                    assert opts.permission_mode == ("bypassPermissions" if trusted else "default")
                    assert opts.sandbox == ({"enabled": False} if trusted else None)
                    assert (opts.can_use_tool is None) == trusted
                    assert opts.resume == sid
                else:
                    cmd, _, _, prompt_file = session._build_command("test")
                    assert prompt_file is None
                    assert cmd[cmd.index("--permission-mode") + 1] == ("bypassPermissions" if trusted else "dontAsk")
                    assert "--always-approve" in cmd
                    assert ("--resume" in cmd) == bool(sid)
print("all CPU permission scenarios passed")
'''
    result = subprocess.run(
        [str(REPO / ".venv/bin/python"), "-c", code], cwd=REPO, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "all CPU permission scenarios passed" in result.stdout


@pytest.mark.parametrize("channel", ["bot.py", "weixin_bot.py"])
def test_channel_service_environment_wins_over_dotenv(tmp_path, monkeypatch, channel):
    from dotenv import load_dotenv

    tree = ast.parse((REPO / channel).read_text())
    call = next(
        node for node in tree.body if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Name) and node.value.func.id == "load_dotenv"
    ).value
    kwargs = {kw.arg: ast.literal_eval(kw.value) for kw in call.keywords}
    env_file = tmp_path / ".env"
    env_file.write_text("BABATA_FULL_TRUST=0\nBABATA_CODEX_SANDBOX=workspace-write\n")
    monkeypatch.setenv("BABATA_FULL_TRUST", "1")
    monkeypatch.setenv("BABATA_CODEX_SANDBOX", "danger-full-access")
    load_dotenv(env_file, **kwargs)
    assert os.environ["BABATA_FULL_TRUST"] == "1"
    assert os.environ["BABATA_CODEX_SANDBOX"] == "danger-full-access"


def test_full_trust_does_not_bypass_wechat_sender_allowlist(monkeypatch):
    import weixin_account

    monkeypatch.setenv("BABATA_FULL_TRUST", "1")
    monkeypatch.delenv("BABATA_WEIXIN_ALLOW_ALL", raising=False)
    monkeypatch.setattr(weixin_account, "load_allow_from", lambda _: ["owner"])
    assert weixin_account.is_allowed("account", "owner") is True
    assert weixin_account.is_allowed("account", "stranger") is False
    monkeypatch.setattr(weixin_account, "load_allow_from", lambda _: [])
    assert weixin_account.is_allowed("account", "owner") is False
