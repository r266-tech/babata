import importlib.util
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

import pytest


REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("full_trust", REPO / "scripts/full-trust.py")
trust = importlib.util.module_from_spec(spec)
spec.loader.exec_module(trust)


@pytest.fixture
def deployment(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    repo = tmp_path / "repo"
    repo.mkdir()
    agents = Path.home() / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    receipt = tmp_path / "receipts" / "run.json"
    receipt.parent.mkdir()
    return repo, agents, receipt


def write_plist(repo, agents, *, label="com.test.weixin", script="weixin_bot.py", shell=False):
    args = [str(repo / ".venv/bin/python"), str(repo / script)]
    if shell:
        args = ["/bin/bash", "-lc", f"set -a; source {repo}/.env.vvv; set +a; exec {' '.join(args)}"]
    data = {"Label": label, "ProgramArguments": args, "EnvironmentVariables": {"KEEP_ME": "fixture"}}
    p = agents / f"{label}.plist"
    p.write_bytes(plistlib.dumps(data))
    return p


@pytest.mark.parametrize("script,label,shell", [
    ("weixin_bot.py", "com.test.weixin", False),
    ("bot.py", "com.test", False),
    ("bot.py", "com.test.vvv", True),
])
def test_valid_channels_preserve_unrelated_config_and_backup(deployment, monkeypatch, script, label, shell):
    repo, agents, receipt = deployment
    p = write_plist(repo, agents, script=script, label=label, shell=shell)
    before = p.read_bytes()
    monkeypatch.setattr(trust, "runtime_state", lambda *_: {"pid": 1, "env": {}})
    reloaded = []
    monkeypatch.setattr(trust, "reload_service", lambda *args: reloaded.append(args[2]))
    monkeypatch.setattr(trust, "wait_ready", lambda *_: {"pid": 2, "env": trust.TRUST_ENV})
    result = trust.apply(repo, "com.test", label, receipt, "launchctl", repo / "state", 0, 0)
    assert result["ok"] is True
    assert result["pid"] == 2
    assert reloaded == [label]
    assert Path(result["backup"]).read_bytes() == before
    assert Path(result["backup"]).stat().st_mode & 0o777 == 0o600
    after = plistlib.loads(p.read_bytes())
    assert after["ProgramArguments"] == plistlib.loads(before)["ProgramArguments"]
    assert after["EnvironmentVariables"] == {"KEEP_ME": "fixture", **trust.TRUST_ENV}
    assert "KEEP_ME" not in json.dumps(result)


@pytest.mark.parametrize("label,script", [
    ("com.test.sidebar-bot", "sidebar_bot.py"),
    ("com.test.cron", "cron.py"),
    ("com.other", "bot.py"),
])
def test_rejects_other_services_without_writing(deployment, label, script):
    repo, agents, _ = deployment
    p = write_plist(repo, agents, label=label, script=script)
    before = p.read_bytes()
    with pytest.raises(ValueError):
        trust.validate(repo, "com.test", label)
    assert p.read_bytes() == before


def test_rejects_wrong_label_shell_injection_and_instance_override(deployment):
    repo, agents, _ = deployment
    p = write_plist(repo, agents, label="com.test.vvv", script="bot.py", shell=True)
    (repo / ".env.vvv").write_text("BABATA_FULL_TRUST=0\n")
    with pytest.raises(ValueError, match="conflicts"):
        trust.validate(repo, "com.test", "com.test.vvv")
    (repo / ".env.vvv").unlink()
    data = plistlib.loads(p.read_bytes())
    data["ProgramArguments"][-1] += "; unexpected-command"
    p.write_bytes(plistlib.dumps(data))
    with pytest.raises(ValueError, match="entrypoint"):
        trust.validate(repo, "com.test", "com.test.vvv")
    data["Label"] = "com.other"
    p.write_bytes(plistlib.dumps(data))
    with pytest.raises(ValueError, match="Label"):
        trust.validate(repo, "com.test", "com.test.vvv")


def test_already_loaded_full_trust_is_idempotent(deployment, monkeypatch):
    repo, agents, receipt = deployment
    p = write_plist(repo, agents)
    data = plistlib.loads(p.read_bytes())
    data["EnvironmentVariables"].update(trust.TRUST_ENV)
    p.write_bytes(plistlib.dumps(data))
    before = p.read_bytes()
    current = {"pid": 1, "env": trust.TRUST_ENV}
    monkeypatch.setattr(trust, "runtime_state", lambda *_: current)
    monkeypatch.setattr(trust, "wait_ready", lambda *_: current)
    monkeypatch.setattr(trust, "reload_service", lambda *_: pytest.fail("unnecessary reload"))
    result = trust.apply(repo, "com.test", "com.test.weixin", receipt, "launchctl", repo / "state", 0, 0)
    assert result["status"] == "unchanged"
    assert p.read_bytes() == before
    assert not receipt.with_suffix(".before.plist").exists()


def test_bootstrap_failure_restores_original_file_and_reloads(deployment, monkeypatch):
    repo, agents, receipt = deployment
    p = write_plist(repo, agents)
    before = p.read_bytes()
    states = iter([{"pid": 1, "env": {}}, {"pid": None, "env": {}}])
    monkeypatch.setattr(trust, "runtime_state", lambda *_: next(states))
    reloads = []
    monkeypatch.setattr(trust, "reload_service", lambda *_: reloads.append(p.read_bytes()))
    def ready(*args):
        if args[2] == trust.TRUST_ENV:
            raise RuntimeError("bootstrap failed")
        return {"pid": 3, "env": {}}
    monkeypatch.setattr(trust, "wait_ready", ready)
    result = trust.apply(repo, "com.test", "com.test.weixin", receipt, "launchctl", repo / "state", 0, 0)
    assert result["ok"] is False
    assert result["rollback"] == "restored"
    assert p.read_bytes() == before
    assert len(reloads) == 2 and reloads[-1] == before


def test_reload_failure_preserves_concurrent_plist_edit(deployment, monkeypatch):
    repo, agents, receipt = deployment
    p = write_plist(repo, agents)
    monkeypatch.setattr(trust, "runtime_state", lambda *_: {"pid": 1, "env": {}})
    monkeypatch.setattr(trust, "reload_service", lambda *_: None)
    def ready(*_):
        p.write_bytes(b"concurrent edit")
        raise RuntimeError("reload failed")
    monkeypatch.setattr(trust, "wait_ready", ready)
    with pytest.raises(RuntimeError, match="concurrently"):
        trust.apply(repo, "com.test", "com.test.weixin", receipt, "launchctl", repo / "state", 0, 0)
    assert p.read_bytes() == b"concurrent edit"


def test_runtime_readback_only_returns_permission_keys(monkeypatch):
    from types import SimpleNamespace
    output = """service = {
    pid = 321
    environment = {
        PRIVATE_TOKEN => do-not-return
        BABATA_FULL_TRUST => 1
        BABATA_CODEX_SANDBOX => danger-full-access
    }
}
"""
    monkeypatch.setattr(trust.subprocess, "run", lambda *_, **__: SimpleNamespace(returncode=0, stdout=output))
    assert trust.runtime_state("launchctl", "com.test") == {
        "pid": 321, "env": {"BABATA_FULL_TRUST": "1", "BABATA_CODEX_SANDBOX": "danger-full-access"},
    }


def shell_deployment(tmp_path):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    for name in ["self-ops.sh", "full-trust.py"]:
        shutil.copy2(REPO / "scripts" / name, repo / "scripts" / name)
    (repo / ".venv/bin").mkdir(parents=True)
    python = repo / ".venv/bin/python"
    python.write_text(f'#!/bin/bash\nexec "{REPO}/.venv/bin/python" "$@"\n')
    python.chmod(0o755)
    home = tmp_path / "home"
    agents = home / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    fake = tmp_path / "launchctl"
    fake.write_text(f"#!{REPO}/.venv/bin/python\n" + '''
import json, os, pathlib, plistlib, subprocess, sys
root = pathlib.Path(os.environ["FAKE_LAUNCH_STATE"])
args = sys.argv[1:]
with (root / "calls").open("a") as f:
    f.write(args[0] + "\\n")
current = root / "current.json"
if args[0] == "submit":
    raise SystemExit(subprocess.run(args[args.index("--") + 1:]).returncode)
if args[0] == "print":
    if not current.exists(): raise SystemExit(1)
    d = json.loads(current.read_text())
    print("service = {\\n    pid = " + str(d["pid"]) + "\\n    environment = {")
    for k, v in d["env"].items(): print("        " + k + " => " + str(v))
    print("    }\\n}")
elif args[0] == "bootout":
    current.unlink(missing_ok=True)
elif args[0] == "bootstrap":
    d = plistlib.loads(pathlib.Path(args[2]).read_bytes())
    current.write_text(json.dumps({"pid": 4243, "env": d["EnvironmentVariables"]}))
elif args[0] != "remove":
    raise SystemExit(2)
''')
    fake.chmod(0o755)
    launch_state = tmp_path / "launch-state"
    launch_state.mkdir()
    (launch_state / "current.json").write_text(json.dumps({"pid": 4242, "env": {}}))
    state = tmp_path / "state"
    state.mkdir()
    env = os.environ.copy()
    env.update({
        "HOME": str(home), "PROJECT_NAMESPACE": "test", "PROJECT_STATE_DIR": str(state),
        "BABATA_LAUNCHCTL": str(fake), "FAKE_LAUNCH_STATE": str(launch_state),
        "RESTART_IDLE_WAIT_SECONDS": "0", "DELAY": "0", "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.pop("SELF_OPS_RESULT_DIR", None)
    return repo, agents, env, state, launch_state


def test_self_ops_dispatch_reload_receipt_and_repeated_call(tmp_path):
    repo, agents, env, state, launch_state = shell_deployment(tmp_path)
    p = write_plist(repo, agents)
    before = p.read_bytes()
    for expected in ["reloaded", "unchanged"]:
        result = subprocess.run(
            ["/bin/bash", str(repo / "scripts/self-ops.sh"), "enable-full-trust", "com.test.weixin"],
            env=env, capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stderr
        receipts = sorted((state / "self-ops/full-trust").glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
        receipt = json.loads(receipts[-1].read_text())
        assert receipt["ok"] is True, receipt
        assert receipt["status"] == expected
    assert (launch_state / "calls").read_text().splitlines().count("bootout") == 1
    backups = list((state / "self-ops/full-trust").glob("*.before.plist"))
    assert len(backups) == 1 and backups[0].read_bytes() == before


def test_self_ops_set_model_and_reasoning_preserves_channel_configuration(tmp_path):
    repo, agents, env, state, launch_state = shell_deployment(tmp_path)
    p = write_plist(repo, agents)
    data = plistlib.loads(p.read_bytes())
    data["EnvironmentVariables"].update({
        **trust.TRUST_ENV,
        "BABATA_CODEX_MODEL": "old-model",
        "BABATA_CODEX_REASONING": "low",
    })
    p.write_bytes(plistlib.dumps(data))
    before = p.read_bytes()
    for expected in ["reloaded", "unchanged"]:
        result = subprocess.run(
            ["/bin/bash", str(repo / "scripts/self-ops.sh"), "set-model",
             "com.test.weixin", "new-model", "high"],
            env=env, capture_output=True, text=True, timeout=20,
        )
        assert result.returncode == 0, result.stderr
        receipts = sorted((state / "self-ops/full-trust").glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
        receipt = json.loads(receipts[-1].read_text())
        assert receipt["ok"] is True, receipt
        assert receipt["status"] == expected
    after = plistlib.loads(p.read_bytes())
    loaded = json.loads((launch_state / "current.json").read_text())["env"]
    assert after["ProgramArguments"] == data["ProgramArguments"]
    assert after["EnvironmentVariables"] == loaded == {
        **data["EnvironmentVariables"],
        "BABATA_CODEX_MODEL": "new-model", "BABATA_CODEX_REASONING": "high",
    }
    assert (launch_state / "calls").read_text().splitlines().count("bootout") == 1
    backups = list((state / "self-ops/full-trust").glob("*.before.plist"))
    assert len(backups) == 1 and backups[0].read_bytes() == before


def test_runtime_readback_tracks_model_without_exposing_other_environment(monkeypatch):
    from types import SimpleNamespace
    output = """service = {
    pid = 321
    environment = {
        PRIVATE_TOKEN => do-not-return
        BABATA_CODEX_MODEL => new-model
        BABATA_CODEX_REASONING => high
    }
}
"""
    monkeypatch.setattr(trust.subprocess, "run", lambda *_, **__: SimpleNamespace(returncode=0, stdout=output))
    assert trust.runtime_state("launchctl", "com.test") == {
        "pid": 321, "env": {"BABATA_CODEX_MODEL": "new-model", "BABATA_CODEX_REASONING": "high"},
    }


@pytest.mark.parametrize("busy_kind", ["wechat_pending", "long_tg_turn", "malformed_status"])
def test_reload_waits_for_pending_wechat_and_long_running_tg(tmp_path, busy_kind):
    repo, agents, env, state, launch_state = shell_deployment(tmp_path)
    label = "com.test.weixin" if busy_kind == "wechat_pending" else "com.test"
    p = write_plist(repo, agents, label=label, script="weixin_bot.py" if busy_kind == "wechat_pending" else "bot.py")
    if busy_kind == "wechat_pending":
        (state / "test-weixin-pending-updates.json").write_text(json.dumps({"pending": {"id": {}}}))
    elif busy_kind == "long_tg_turn":
        (state / "runtime-status-.json").write_text(json.dumps({"pid": os.getpid(), "in_flight": 1, "ts": 0}))
    else:
        (state / "runtime-status-.json").write_text("invalid-json")
    result = subprocess.run(
        ["/bin/bash", str(repo / "scripts/self-ops.sh"), "reload-plist", label, str(p)],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
    assert "bootout" not in (launch_state / "calls").read_text().splitlines()
