"""Private channel deployment transaction; invoked by self-ops.sh only.

Keep credentials out of output. The receipt contains service state and paths,
never complete launchd environments or plist contents.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import subprocess
import tempfile
import time

from dotenv import dotenv_values


TRUST_ENV = {
    "BABATA_FULL_TRUST": "1",
    "BABATA_CODEX_SANDBOX": "danger-full-access",
    "BABATA_GROK_PERMISSION_MODE": "bypassPermissions",
    "BABATA_GROK_ALWAYS_APPROVE": "1",
}
TRACKED_ENV = {*TRUST_ENV, "BABATA_CODEX_REASONING", "BABATA_CODEX_MODEL"}


def _shell_tokens(command: str) -> list[str]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";")
    lexer.whitespace_split = True
    return list(lexer)


def validate(repo: Path, prefix: str, label: str, updates: dict | None = None) -> tuple[Path, bytes, dict]:
    updates = TRUST_ENV if updates is None else updates
    if not re.fullmatch(r"[A-Za-z0-9._-]+", label) or not (
        label == prefix or label.startswith(prefix + ".")
    ):
        raise ValueError("label is outside this project namespace")
    plist = Path.home() / "Library/LaunchAgents" / f"{label}.plist"
    if plist.is_symlink() or plist.parent.is_symlink():
        raise ValueError("channel plist must not be a symlink")
    before = plist.read_bytes()
    data = plistlib.loads(before)
    if data.get("Label") != label:
        raise ValueError("plist Label does not match the requested service")
    args = data.get("ProgramArguments")
    python = str(repo / ".venv/bin/python")
    script = repo / ("weixin_bot.py" if label == prefix + ".weixin" else "bot.py")
    if args != [python, str(script)]:
        instance = label.removeprefix(prefix + ".")
        env_file = repo / f".env.{instance}"
        expected = (
            f"set -a; source {shlex.quote(str(env_file))}; set +a; "
            f"exec {shlex.quote(python)} {shlex.quote(str(script))}"
        )
        if not (
            script.name == "bot.py" and isinstance(args, list) and len(args) == 3
            and args[0] in {"/bin/bash", "/bin/zsh"} and args[1] in {"-c", "-lc"}
            and _shell_tokens(args[2]) == _shell_tokens(expected)
        ):
            raise ValueError("service is not a supported TG or WeChat entrypoint in this repo")
        # A shell source runs before Python's dotenv loader and can replace
        # launchd values. Reject conflicting overrides instead of silently
        # deploying a configuration that cannot take effect.
        instance_env = dotenv_values(env_file)
        if any(k in instance_env and instance_env[k] != v for k, v in updates.items()):
            raise ValueError("instance dotenv conflicts with requested service configuration")
    return plist, before, data


def atomic_write(path: Path, content: bytes, mode: int = 0o600) -> None:
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def runtime_state(launchctl: str, label: str) -> dict:
    result = subprocess.run(
        [launchctl, "print", f"gui/{os.getuid()}/{label}"],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode:
        return {"pid": None, "env": {}}
    pid = re.search(r"(?m)^\s*pid = (\d+)\s*$", result.stdout)
    environment = re.search(r"(?ms)^\s*environment = \{\n(.*?)^\s*\}", result.stdout)
    values = {}
    if environment:
        for line in environment[1].splitlines():
            key, sep, value = line.strip().partition(" => ")
            if sep and key in TRACKED_ENV:
                values[key] = value.strip('"')
    return {"pid": int(pid[1]) if pid else None, "env": values}


def wait_ready(launchctl: str, label: str, expected: dict, old_pid: int | None, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    stable_pid = None
    stable_count = 0
    while True:
        state = runtime_state(launchctl, label)
        pid = state["pid"]
        if pid and pid != old_pid and state["env"] == expected:
            stable_count = stable_count + 1 if stable_pid == pid else 1
            stable_pid = pid
            if stable_count >= 3:
                return state
        else:
            stable_count = 0
        if time.monotonic() >= deadline:
            raise RuntimeError("service did not reach a stable PID with the expected loaded permissions")
        time.sleep(1)


def reload_service(repo: Path, prefix: str, label: str, plist: Path, launchctl: str, state: Path, wait: int, delay: int) -> None:
    env = os.environ.copy()
    env.update({
        "BABATA_LAUNCHCTL": launchctl,
        "PROJECT_NAMESPACE": prefix.removeprefix("com."),
        "PROJECT_STATE_DIR": str(state),
        "RESTART_IDLE_WAIT_SECONDS": str(wait),
        "DELAY": str(delay),
    })
    result = subprocess.run(
        ["/bin/bash", str(repo / "scripts/self-ops.sh"), "reload-plist", label, str(plist)],
        env=env, capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError("self-ops could not queue the service reload")


def apply(repo: Path, prefix: str, label: str, receipt: Path, launchctl: str, state: Path, wait: int, delay: int, updates: dict | None = None) -> dict:
    updates = TRUST_ENV if updates is None else updates
    plist, before, data = validate(repo, prefix, label, updates)
    initial = runtime_state(launchctl, label)
    if not initial["pid"]:
        raise ValueError("channel service must already be running before changing configuration")
    data.setdefault("EnvironmentVariables", {}).update(updates)
    expected = {k: str(v) for k, v in data["EnvironmentVariables"].items() if k in TRACKED_ENV}
    desired = plistlib.dumps(data)
    if plistlib.loads(before) == data and initial["env"] == expected:
        ready = wait_ready(launchctl, label, expected, None, 10)
        return {"ok": True, "status": "unchanged", "label": label, "pid": ready["pid"]}

    backup = receipt.with_suffix(".before.plist")
    mode = plist.stat().st_mode & 0o777
    atomic_write(backup, before)
    if plist.read_bytes() != before:
        raise RuntimeError("plist changed during preparation; no configuration was replaced")
    atomic_write(plist, desired, mode)
    try:
        reload_service(repo, prefix, label, plist, launchctl, state, wait, delay)
        ready = wait_ready(launchctl, label, expected, initial["pid"], wait + delay + 45)
        return {"ok": True, "status": "reloaded", "label": label, "pid": ready["pid"], "backup": str(backup)}
    except Exception as exc:
        if plist.read_bytes() != desired:
            raise RuntimeError("reload failed and plist changed concurrently; preserved current file and backup") from exc
        atomic_write(plist, before, mode)
        current = runtime_state(launchctl, label)
        rollback = "restored"
        try:
            if current != initial:
                reload_service(repo, prefix, label, plist, launchctl, state, wait, delay)
                wait_ready(launchctl, label, initial["env"], current["pid"], wait + delay + 45)
            else:
                wait_ready(launchctl, label, initial["env"], None, 10)
        except Exception:
            rollback = "configuration_restored_service_needs_attention"
        return {"ok": False, "status": "reload_failed", "label": label, "rollback": rollback, "backup": str(backup), "error": str(exc)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["validate", "apply"])
    parser.add_argument("repo", type=Path)
    parser.add_argument("prefix")
    parser.add_argument("label")
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--launchctl", default="launchctl")
    parser.add_argument("--wait", type=int, default=3600)
    parser.add_argument("--delay", type=int, default=5)
    parser.add_argument("--reasoning", choices=["", "low", "medium", "high", "xhigh", "max", "ultra"], default="")
    parser.add_argument("--model", default="")
    args = parser.parse_args()
    if args.model and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", args.model):
        parser.error("model must be a simple model ID")
    updates = {}
    if args.model:
        updates["BABATA_CODEX_MODEL"] = args.model
    if args.reasoning:
        updates["BABATA_CODEX_REASONING"] = args.reasoning
    if not updates:
        updates = TRUST_ENV
    if args.action == "validate":
        try:
            validate(args.repo, args.prefix, args.label, updates)
            return 0
        except Exception as exc:
            print(f"ERR: {exc}")
            return 1
    if args.receipt is None or args.state is None:
        parser.error("apply requires --receipt and --state")
    os.umask(0o077)
    try:
        with (args.receipt.parent / f"{args.label}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = apply(args.repo, args.prefix, args.label, args.receipt, args.launchctl, args.state, args.wait, args.delay, updates)
    except Exception as exc:
        result = {"ok": False, "status": "failed", "label": args.label, "error": str(exc)}
    atomic_write(args.receipt, (json.dumps(result, sort_keys=True) + "\n").encode())
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
