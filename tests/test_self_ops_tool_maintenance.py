import json
import os
from pathlib import Path
import subprocess
import sys


SELF_OPS = Path(__file__).resolve().parents[1] / "scripts/self-ops.sh"


def setup_owner(tmp_path, launchctl_exit=0):
    runner = tmp_path / "tool owner"
    marker = tmp_path / "executed"
    runner.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    runner.chmod(0o755)
    capture = tmp_path / "dispatch.json"
    launchctl = tmp_path / "launchctl"
    launchctl.write_text(
        f"#!{sys.executable}\nimport json,pathlib,sys\n"
        "if sys.argv[1] == 'print': raise SystemExit(1)\n"
        f"pathlib.Path({str(capture)!r}).write_text(json.dumps(sys.argv[1:]))\n"
        f"raise SystemExit({launchctl_exit})\n"
    )
    launchctl.chmod(0o755)
    env = {
        **os.environ,
        "PROJECT_STATE_DIR": str(tmp_path / "state"),
        "BABATA_LAUNCHCTL": str(launchctl),
        "BABATA_TOOL_MAINTAIN_BIN": str(runner),
        "BABATA_TOOL_MAINTAIN_LOG_DIR": str(tmp_path / "logs"),
        "BABATA_TOOL_MAINTAIN_STATE_DIR": str(tmp_path / "tool-state"),
    }
    return runner, marker, capture, env


def test_tool_update_dispatches_fixed_owner_without_inline_execution(tmp_path):
    runner, marker, capture, env = setup_owner(tmp_path)
    result = subprocess.run(
        ["bash", str(SELF_OPS), "update-tools"], env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    argv = json.loads(capture.read_text())
    assert argv[:2] == ["submit", "-l"]
    assert str(runner) in argv
    assert argv[argv.index("--") + 1:argv.index("--") + 3] == ["/bin/bash", "-c"]
    assert '"$1" run' in argv[argv.index("--") + 3]
    assert "读取实际结果" in result.stdout


def test_tool_update_rejects_arbitrary_arguments(tmp_path):
    _, _, capture, env = setup_owner(tmp_path)
    result = subprocess.run(
        ["bash", str(SELF_OPS), "update-tools", "--anything"], env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 2
    assert not capture.exists()


def test_detached_worker_marks_its_owner_dispatch_and_preserves_selection(tmp_path):
    runner, marker, capture, env = setup_owner(tmp_path)
    runner.write_text(
        f"#!{sys.executable}\nimport json,os,pathlib,sys\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{"
        "'dispatch': os.environ.get('BABATA_TOOL_MAINTAIN_DISPATCHED'), "
        "'args': sys.argv[1:]}))\n"
    )
    result = subprocess.run(
        ["bash", str(SELF_OPS), "update-tools", "--only", "uv", "--only", "npm:package:/path with spaces"], env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    argv = json.loads(capture.read_text())
    worker = subprocess.run(
        argv[argv.index("--") + 1:], env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert worker.returncode == 0, worker.stderr
    assert json.loads(marker.read_text()) == {
        "dispatch": "1",
        "args": ["run", "--only", "uv", "--only", "npm:package:/path with spaces"],
    }


def test_tool_update_reports_dispatch_failure(tmp_path):
    _, marker, _, env = setup_owner(tmp_path, launchctl_exit=42)
    result = subprocess.run(
        ["bash", str(SELF_OPS), "update-tools"], env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1
    assert "dispatch failed" in result.stderr
    assert not marker.exists()


def test_same_selection_is_deduplicated_before_dispatch(tmp_path):
    _, marker, capture, env = setup_owner(tmp_path)
    launchctl = Path(env['BABATA_LAUNCHCTL'])
    state = tmp_path / 'active-job'
    count = tmp_path / 'submissions'
    launchctl.write_text(f'''#!{sys.executable}
import pathlib,sys
state=pathlib.Path({str(state)!r})
count=pathlib.Path({str(count)!r})
if sys.argv[1]=='print': raise SystemExit(0 if state.exists() and state.read_text()==sys.argv[2].split('/')[-1] else 1)
if sys.argv[1]=='submit':
 state.write_text(sys.argv[sys.argv.index('-l')+1])
 with count.open('a') as out: out.write('submit\\n')
''')
    for selected in (['brew:gh', 'brew:jq'], ['brew:jq', 'brew:gh']):
        argv = ['bash', str(SELF_OPS), 'update-tools']
        for identity in selected: argv += ['--only', identity]
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
    assert count.read_text() == 'submit\n'
    assert '已合并' in result.stdout and not marker.exists()


def test_coordinated_hold_prevents_new_dispatch(tmp_path):
    _, marker, capture, env = setup_owner(tmp_path)
    state = Path(env["BABATA_TOOL_MAINTAIN_STATE_DIR"])
    state.mkdir()
    (state / "hold.json").write_text('{"reason":"runtime_recovery"}')
    result = subprocess.run(
        ["bash", str(SELF_OPS), "update-tools"], env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 75
    assert not capture.exists() and not marker.exists()


def test_scoped_hold_allows_only_the_explicit_independent_owner(tmp_path):
    _, marker, capture, env = setup_owner(tmp_path)
    state = Path(env["BABATA_TOOL_MAINTAIN_STATE_DIR"])
    state.mkdir()
    (state / "hold.json").write_text(json.dumps({
        "owner": "runtime-owner", "reason": "dependency recovery",
        "created_at": "2026-09-08T08:00:00+00:00", "allowed_managers": ["npm"],
    }))
    for selection in ([], ["--only", "npm", "--only", "brew:gh"]):
        result = subprocess.run(
            ["bash", str(SELF_OPS), "update-tools", *selection], env=env,
            capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 75
        assert not capture.exists() and not marker.exists()
    result = subprocess.run(
        ["bash", str(SELF_OPS), "update-tools", "--only", "npm"], env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert json.loads(capture.read_text())[-2:] == ["--only", "npm"]


def test_tool_update_timeout_reaches_detached_owner_and_request_identity(tmp_path):
    runner, marker, capture, env = setup_owner(tmp_path)
    runner.write_text(
        f"#!{sys.executable}\nimport json,os,pathlib,sys\n"
        f"pathlib.Path({str(marker)!r}).write_text(json.dumps({{"
        "'dispatch': os.environ.get('BABATA_TOOL_MAINTAIN_DISPATCHED'), "
        "'args': sys.argv[1:]}))\n"
    )
    labels = []
    for args in (
        ["--only", "brew:go", "--timeout", "1800"],
        ["--timeout", "1800", "--only", "brew:go"],
        ["--only", "brew:go", "--timeout", "3600"],
    ):
        result = subprocess.run(
            ["/bin/bash", str(SELF_OPS), "update-tools", *args],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 0, result.stderr
        argv = json.loads(capture.read_text())
        labels.append(argv[argv.index("-l") + 1])
        worker = subprocess.run(
            argv[argv.index("--") + 1:], env=env,
            capture_output=True, text=True, timeout=10,
        )
        assert worker.returncode == 0, worker.stderr
        actual = json.loads(marker.read_text())
        assert actual['dispatch'] == '1'
        assert actual['args'] == ["run", "--only", "brew:go", "--timeout", args[args.index("--timeout") + 1]]
    assert labels[0] == labels[1]
    assert labels[0] != labels[2]


def test_tool_update_rejects_invalid_timeout_before_dispatch(tmp_path):
    _, marker, capture, env = setup_owner(tmp_path)
    for args in (
        ["--timeout"], ["--timeout", "0"], ["--timeout", "-1"],
        ["--timeout", "1.5"], ["--timeout", "nope"],
        ["--timeout", "1800", "--timeout", "3600"],
    ):
        result = subprocess.run(
            ["/bin/bash", str(SELF_OPS), "update-tools", *args],
            env=env, capture_output=True, text=True, timeout=10,
        )
        assert result.returncode == 2, (args, result.stderr)
        assert not capture.exists() and not marker.exists()


def test_tool_update_timeout_preserves_scope_hold(tmp_path):
    _, marker, capture, env = setup_owner(tmp_path)
    state = Path(env["BABATA_TOOL_MAINTAIN_STATE_DIR"])
    state.mkdir()
    (state / "hold.json").write_text(json.dumps({
        "owner": "runtime-owner", "reason": "dependency recovery",
        "created_at": "2026-09-08T08:00:00+00:00", "allowed_managers": ["npm"],
    }))
    result = subprocess.run(
        ["/bin/bash", str(SELF_OPS), "update-tools", "--only", "brew:go", "--timeout", "1800"],
        env=env, capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 75
    assert not capture.exists() and not marker.exists()
