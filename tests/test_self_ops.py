import os
import json
import plistlib
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SELF_OPS = REPO / "scripts" / "self-ops.sh"


@pytest.mark.parametrize("dry_run, uv_exit", [(False, 0), (True, 0), (False, 29)])
def test_sync_wx_deps_uses_only_lock_pins_and_preserves_uv_failure(tmp_path, dry_run, uv_exit):
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    script = repo / "scripts" / "self-ops.sh"
    script.write_text(SELF_OPS.read_text())
    (repo / ".venv" / "bin").mkdir(parents=True)
    (repo / ".venv" / "bin" / "python").symlink_to(sys.executable)
    lock = repo / "uv.lock"
    lock.write_text('''[[package]]
name = "pilk"
version = "0.2.4"
[[package]]
name = "qrcode"
version = "8.2"
[[package]]
name = "claude-agent-sdk"
version = "0.0.1"
''')
    original_lock = lock.read_bytes()
    fake_uv = tmp_path / "uv"
    _write_executable(fake_uv, f"#!{sys.executable}\n" + '''import json, os, pathlib, sys
args = sys.argv[1:]
requirements = pathlib.Path(args[args.index("--requirements") + 1])
pathlib.Path(os.environ["FAKE_UV_CAPTURE"]).write_text(json.dumps({
    "args": args,
    "requirements": requirements.read_text(),
    "indexes": {key: os.environ.get(key) for key in (
        "UV_INDEX_URL", "PIP_INDEX_URL", "UV_EXTRA_INDEX_URL", "PIP_EXTRA_INDEX_URL")},
}))
raise SystemExit(int(os.environ["FAKE_UV_EXIT"]))
''')
    capture = tmp_path / "uv-call.json"
    env = os.environ.copy()
    env.update({
        "BABATA_UV": str(fake_uv), "FAKE_UV_CAPTURE": str(capture),
        "FAKE_UV_EXIT": str(uv_exit), "PROJECT_STATE_DIR": str(tmp_path / "state"),
        **{key: "https://invalid.example/simple" for key in (
            "UV_INDEX_URL", "PIP_INDEX_URL", "UV_EXTRA_INDEX_URL", "PIP_EXTRA_INDEX_URL")},
    })
    result = subprocess.run(
        ["/bin/bash", str(script), "sync-wx-deps", *(["--dry-run"] if dry_run else [])],
        cwd=repo, env=env, capture_output=True, text=True, check=False, timeout=10,
    )

    assert result.returncode == uv_exit, result.stderr
    call = json.loads(capture.read_text())
    assert call["requirements"].splitlines() == ["pilk==0.2.4", "qrcode==8.2"]
    assert call["args"][:3] == ["--no-config", "pip", "install"]
    assert "--no-deps" in call["args"]
    assert ("--dry-run" in call["args"]) is dry_run
    assert call["args"][call["args"].index("--python") + 1] == str(repo / ".venv/bin/python")
    assert call["args"][call["args"].index("--index-url") + 1] == "https://pypi.org/simple"
    assert all(value is None for value in call["indexes"].values())
    assert lock.read_bytes() == original_lock
    assert not Path(call["args"][call["args"].index("--requirements") + 1]).exists()


def _write_executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(0o755)


def _setup(tmp_path: Path, *, label: str = "com.v.babata-secretary") -> tuple[Path, dict[str, str], Path]:
    home = tmp_path / "home"
    launch_agents = home / "Library" / "LaunchAgents"
    launch_agents.mkdir(parents=True)
    plist = launch_agents / f"{label}.plist"
    with (Path(f"{plist}.disabled")).open("wb") as handle:
        plistlib.dump({"Label": label, "ProgramArguments": ["/bin/true"]}, handle)

    state = tmp_path / "launchctl-state"
    state.mkdir()
    fake_launchctl = tmp_path / "launchctl"
    _write_executable(
        fake_launchctl,
        """#!/usr/bin/env bash
set -u
printf '%s\n' "$*" >> "$FAKE_LAUNCHCTL_STATE/calls.log"
case "${1:-}" in
  submit)
    shift
    while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done
    [ "$#" -gt 0 ] || exit 2
    shift
    if [ "${FAKE_MODEL_KEEPALIVE:-0}" = "1" ]; then
      "$@"
      rc=$?
      if [ "$rc" -ne 0 ]; then
        : > "$FAKE_LAUNCHCTL_STATE/keepalive-restarted"
        "$@"
        rc=$?
      fi
      exit "$rc"
    fi
    exec "$@"
    ;;
  print)
    if [ "${FAKE_SWAP_DISABLED_ON_PRINT:-0}" = "1" ] && [ ! -f "$FAKE_LAUNCHCTL_STATE/swapped" ]; then
      if [ -n "${FAKE_SWAP_REPLACEMENT:-}" ]; then
        cp "$FAKE_SWAP_REPLACEMENT" "$FAKE_EXPECT_ACTIVE_PLIST.disabled"
      else
        printf 'changed\n' > "$FAKE_EXPECT_ACTIVE_PLIST.disabled"
      fi
      : > "$FAKE_LAUNCHCTL_STATE/swapped"
    fi
    [ -f "$FAKE_LAUNCHCTL_STATE/loaded" ]
    ;;
  enable)
    [ "${FAKE_ENABLE_FAIL:-0}" != "1" ] || exit 41
    [ -z "${FAKE_EXPECT_ACTIVE_PLIST:-}" ] || [ -f "$FAKE_EXPECT_ACTIVE_PLIST" ] || exit 45
    rm -f "$FAKE_LAUNCHCTL_STATE/disabled"
    : > "$FAKE_LAUNCHCTL_STATE/enabled"
    if [ -n "${FAKE_SWAP_ACTIVE_ON_ENABLE:-}" ]; then
      cp "$FAKE_SWAP_ACTIVE_ON_ENABLE" "$FAKE_EXPECT_ACTIVE_PLIST"
    fi
    ;;
  disable)
    rm -f "$FAKE_LAUNCHCTL_STATE/enabled"
    : > "$FAKE_LAUNCHCTL_STATE/disabled"
    if [ "${FAKE_RECREATE_DISABLED_ON_DISABLE:-0}" = "1" ]; then
      cp "$FAKE_EXPECT_ACTIVE_PLIST" "$FAKE_EXPECT_ACTIVE_PLIST.disabled"
    fi
    ;;
  bootstrap)
    if [ "${FAKE_BOOTSTRAP_FAIL:-0}" = "1" ]; then
      [ "${FAKE_BOOTSTRAP_PARTIAL_LOAD:-0}" != "1" ] || : > "$FAKE_LAUNCHCTL_STATE/loaded"
      exit 42
    fi
    [ -f "${3:-}" ] || exit 43
    if [ "${FAKE_VERIFY_FAIL:-0}" != "1" ]; then
      : > "$FAKE_LAUNCHCTL_STATE/loaded"
    fi
    if [ "${FAKE_SIGNAL_AFTER_BOOTSTRAP:-0}" = "1" ]; then
      kill -TERM "$PPID"
    fi
    ;;
  bootout)
    if [ "${FAKE_BOOTOUT_STICKY:-0}" != "1" ]; then
      rm -f "$FAKE_LAUNCHCTL_STATE/loaded"
    fi
    ;;
  kickstart)
    [ "${FAKE_KICKSTART_FAIL:-0}" != "1" ] || exit 46
    ;;
  remove)
    [ "${FAKE_REMOVE_FAIL:-0}" != "1" ] || exit 44
    ;;
  *) exit 2 ;;
esac
""",
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "PROJECT_STATE_DIR": str(tmp_path / "project-state"),
            "BABATA_LAUNCHCTL": str(fake_launchctl),
            "FAKE_LAUNCHCTL_STATE": str(state),
            "FAKE_EXPECT_ACTIVE_PLIST": str(plist),
            "DELAY": "0",
            "ENABLE_ROLLBACK_WAIT_SECONDS": "0",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return plist, env, state


def _run_with_synchronous_submit(
    label: str, plist: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    # The fake launchctl executes a submitted worker synchronously so worker
    # branches are testable. Real launchctl submit only confirms queueing.
    stdout_path = plist.parent / "self-ops.stdout"
    stderr_path = plist.parent / "self-ops.stderr"
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        result = subprocess.run(
            ["/bin/bash", str(SELF_OPS), "enable-plist", label, str(plist)],
            cwd=REPO,
            env=env,
            text=True,
            stdout=stdout,
            stderr=stderr,
            timeout=10,
            check=False,
        )
    return subprocess.CompletedProcess(
        result.args,
        result.returncode,
        stdout_path.read_text(encoding="utf-8"),
        stderr_path.read_text(encoding="utf-8"),
    )


def _worker_result(env: dict[str, str]) -> dict[str, str]:
    result_dir = Path(env["PROJECT_STATE_DIR"]) / "self-ops"
    result_files = list(result_dir.glob("*.result"))
    assert len(result_files) == 1
    return dict(
        line.split("=", 1)
        for line in result_files[0].read_text(encoding="utf-8").splitlines()
    )


def test_kickstart_system_routes_exact_target_through_launchctl(tmp_path: Path) -> None:
    _, env, state = _setup(tmp_path)
    label = "NetworkExtension.io.tailscale.ipn.macsys.network-extension.1.98.5.101.98.5"

    result = subprocess.run(
        ["/bin/bash", str(SELF_OPS), "kickstart-system", label],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"kickstart -k system/{label}" in (
        state / "calls.log"
    ).read_text(encoding="utf-8")


@pytest.mark.parametrize("label", ["gui/501/com.example", "bad label", "../bad"])
def test_kickstart_system_rejects_invalid_label(tmp_path: Path, label: str) -> None:
    _, env, state = _setup(tmp_path)

    result = subprocess.run(
        ["/bin/bash", str(SELF_OPS), "kickstart-system", label],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )

    assert result.returncode != 0
    assert "invalid system launchd label" in result.stderr
    assert not (state / "calls.log").exists()


def test_enable_plist_restores_enables_and_bootstraps(tmp_path: Path) -> None:
    label = "com.v.babata-secretary"
    plist, env, state = _setup(tmp_path, label=label)

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode == 0, result.stderr
    worker = _worker_result(env)
    assert worker["status"] == "success"
    assert worker["stage"] == "loaded"
    assert worker["active_sha"] == worker["expected_sha"]
    assert worker["helper_cleanup"] == "queued"
    assert plist.is_file()
    assert not Path(f"{plist}.disabled").exists()
    assert (state / "enabled").is_file()
    assert (state / "loaded").is_file()
    calls = (state / "calls.log").read_text(encoding="utf-8")
    assert f"enable gui/{os.getuid()}/{label}" in calls
    assert f"bootstrap gui/{os.getuid()} {plist}" in calls
    assert calls.index(f"enable gui/{os.getuid()}/{label}") < calls.index(
        f"bootstrap gui/{os.getuid()} {plist}"
    ) < calls.rindex(f"print gui/{os.getuid()}/{label}")


def test_enable_plist_rolls_back_when_enable_fails(tmp_path: Path) -> None:
    label = "com.v.babata-secretary"
    plist, env, state = _setup(tmp_path, label=label)
    env["FAKE_ENABLE_FAIL"] = "1"

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode == 0
    assert _worker_result(env)["stage"] == "enable_failed"
    assert "failed to enable" in result.stderr
    assert not plist.exists()
    assert Path(f"{plist}.disabled").is_file()
    assert (state / "disabled").is_file()


def test_enable_plist_rolls_back_when_bootstrap_fails(tmp_path: Path) -> None:
    label = "com.v.babata-secretary"
    plist, env, state = _setup(tmp_path, label=label)
    env["FAKE_BOOTSTRAP_FAIL"] = "1"

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode == 0
    assert _worker_result(env)["stage"] == "bootstrap_failed"
    assert "failed to bootstrap" in result.stderr
    assert not plist.exists()
    assert Path(f"{plist}.disabled").is_file()
    assert (state / "disabled").is_file()
    assert not (state / "loaded").exists()
    calls = (state / "calls.log").read_text(encoding="utf-8")
    assert calls.index(f"disable gui/{os.getuid()}/{label}") < calls.index(
        f"bootout gui/{os.getuid()}/{label}"
    )


def test_enable_plist_rolls_back_when_loaded_verification_fails(tmp_path: Path) -> None:
    label = "com.v.babata-secretary"
    plist, env, state = _setup(tmp_path, label=label)
    env["FAKE_VERIFY_FAIL"] = "1"

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode == 0
    assert _worker_result(env)["stage"] == "loaded_verification_failed"
    assert "not loaded after bootstrap" in result.stderr
    assert not plist.exists()
    assert Path(f"{plist}.disabled").is_file()
    assert (state / "disabled").is_file()


def test_enable_plist_preserves_active_plist_when_rollback_cannot_unload(tmp_path: Path) -> None:
    label = "com.v.babata-secretary"
    plist, env, state = _setup(tmp_path, label=label)
    env.update(
        {
            "FAKE_BOOTSTRAP_FAIL": "1",
            "FAKE_BOOTSTRAP_PARTIAL_LOAD": "1",
            "FAKE_BOOTOUT_STICKY": "1",
        }
    )

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode == 0
    assert _worker_result(env)["stage"] == "rollback_incomplete"
    assert "rollback incomplete" in result.stderr
    assert "active plist preserved" in result.stderr
    assert plist.is_file()
    assert not Path(f"{plist}.disabled").exists()
    assert (state / "disabled").is_file()
    assert (state / "loaded").is_file()


def test_enable_plist_deduplicates_expected_plists_during_rollback(tmp_path: Path) -> None:
    label = "com.v.babata-secretary"
    plist, env, _state = _setup(tmp_path, label=label)
    env.update({"FAKE_ENABLE_FAIL": "1", "FAKE_RECREATE_DISABLED_ON_DISABLE": "1"})

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode == 0
    assert _worker_result(env)["stage"] == "enable_failed"
    assert not plist.exists()
    assert Path(f"{plist}.disabled").is_file()
    with Path(f"{plist}.disabled").open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"] == ["/bin/true"]


def test_enable_plist_rejects_content_swapped_after_submit(tmp_path: Path) -> None:
    label = "com.v.babata-secretary"
    plist, env, state = _setup(tmp_path, label=label)
    env["FAKE_SWAP_DISABLED_ON_PRINT"] = "1"

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode == 0
    assert _worker_result(env)["stage"] == "identity_changed"
    assert "identity changed" in result.stderr
    assert not plist.exists()
    assert Path(f"{plist}.disabled").read_text(encoding="utf-8") == "changed\n"
    calls = (state / "calls.log").read_text(encoding="utf-8")
    assert f"enable gui/{os.getuid()}/{label}" not in calls
    assert f"bootstrap gui/{os.getuid()} {plist}" not in calls


def test_enable_plist_rechecks_label_after_preflight_fingerprint_race(tmp_path: Path) -> None:
    label = "com.v.babata-secretary"
    plist, env, state = _setup(tmp_path, label=label)
    replacement = tmp_path / "replacement.plist"
    with replacement.open("wb") as handle:
        plistlib.dump({"Label": "com.v.other", "ProgramArguments": ["/bin/false"]}, handle)
    env.update(
        {
            "FAKE_SWAP_DISABLED_ON_PRINT": "1",
            "FAKE_SWAP_REPLACEMENT": str(replacement),
        }
    )

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode == 0
    assert _worker_result(env)["stage"] == "identity_changed"
    assert "identity changed" in result.stderr
    assert not plist.exists()
    with Path(f"{plist}.disabled").open("rb") as handle:
        assert plistlib.load(handle)["Label"] == "com.v.other"
    calls = (state / "calls.log").read_text(encoding="utf-8")
    assert f"enable gui/{os.getuid()}/{label}" not in calls
    assert f"bootstrap gui/{os.getuid()} {plist}" not in calls


@pytest.mark.parametrize("wait_value", ["invalid", "301"])
def test_enable_plist_rejects_invalid_rollback_wait_before_submit(
    tmp_path: Path, wait_value: str
) -> None:
    label = "com.v.babata-secretary"
    plist, env, state = _setup(tmp_path, label=label)
    env["ENABLE_ROLLBACK_WAIT_SECONDS"] = wait_value

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode != 0
    assert "must be an integer from 0 to 300" in result.stderr
    assert not (state / "calls.log").exists()
    assert not plist.exists()
    assert Path(f"{plist}.disabled").is_file()


def test_enable_plist_rechecks_active_snapshot_before_bootstrap(tmp_path: Path) -> None:
    label = "com.v.babata-secretary"
    plist, env, state = _setup(tmp_path, label=label)
    replacement = tmp_path / "replacement.plist"
    with replacement.open("wb") as handle:
        plistlib.dump({"Label": label, "ProgramArguments": ["/bin/false"]}, handle)
    env["FAKE_SWAP_ACTIVE_ON_ENABLE"] = str(replacement)

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode == 0
    assert _worker_result(env)["stage"] == "active_identity_changed"
    assert not plist.exists()
    assert Path(f"{plist}.disabled").is_file()
    with Path(f"{plist}.disabled").open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"] == ["/bin/true"]
    rejected = list(plist.parent.glob(f"{plist.name}.enable-rejected.*.active"))
    assert len(rejected) == 1
    with rejected[0].open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"] == ["/bin/false"]
    calls = (state / "calls.log").read_text(encoding="utf-8")
    assert f"bootstrap gui/{os.getuid()} {plist}" not in calls


def test_enable_plist_rolls_back_unexpected_exit_after_bootstrap(tmp_path: Path) -> None:
    label = "com.v.babata-secretary"
    plist, env, state = _setup(tmp_path, label=label)
    env.update({"FAKE_SIGNAL_AFTER_BOOTSTRAP": "1", "FAKE_REMOVE_FAIL": "1"})

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode == 0
    worker = _worker_result(env)
    assert worker["status"] == "error"
    assert worker["stage"] == "unexpected_exit_rolled_back"
    assert worker["helper_cleanup"] == "failed"
    assert not plist.exists()
    assert Path(f"{plist}.disabled").is_file()
    with Path(f"{plist}.disabled").open("rb") as handle:
        assert plistlib.load(handle)["ProgramArguments"] == ["/bin/true"]
    assert (state / "disabled").is_file()
    assert not (state / "loaded").exists()


def test_enable_worker_never_triggers_submit_failure_keepalive(tmp_path: Path) -> None:
    label = "com.v.babata-secretary"
    plist, env, state = _setup(tmp_path, label=label)
    env.update(
        {
            "FAKE_BOOTSTRAP_FAIL": "1",
            "FAKE_MODEL_KEEPALIVE": "1",
            "FAKE_REMOVE_FAIL": "1",
        }
    )

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode == 0
    worker = _worker_result(env)
    assert worker["stage"] == "bootstrap_failed"
    assert worker["helper_cleanup"] == "failed"
    assert not (state / "keepalive-restarted").exists()
    assert not plist.exists()
    assert Path(f"{plist}.disabled").is_file()


def test_enable_plist_rejects_mismatched_label(tmp_path: Path) -> None:
    label = "com.v.babata-secretary"
    plist, env, state = _setup(tmp_path, label=label)
    with Path(f"{plist}.disabled").open("wb") as handle:
        plistlib.dump({"Label": "com.v.other"}, handle)

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode != 0
    assert "expected com.v.babata-secretary" in result.stderr
    assert not (state / "calls.log").exists()


def test_enable_plist_rejects_already_loaded_job(tmp_path: Path) -> None:
    label = "com.v.babata-secretary"
    plist, env, state = _setup(tmp_path, label=label)
    (state / "loaded").touch()

    result = _run_with_synchronous_submit(label, plist, env)

    assert result.returncode == 0
    assert _worker_result(env)["stage"] == "preflight_loaded"
    assert "already loaded" in result.stderr
    assert not plist.exists()
    assert Path(f"{plist}.disabled").is_file()
