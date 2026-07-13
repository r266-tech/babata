import ast
import os
import re
import shutil
import subprocess
import time
import tomllib
from pathlib import Path


def test_auto_update_has_single_canonical_implementation():
    repo = Path(__file__).resolve().parents[1]
    root_entry = repo / "auto-update.sh"
    canonical = repo / "scripts" / "auto-update.sh"
    self_ops = repo / "scripts" / "self-ops.sh"
    poll_healthcheck = repo / "scripts" / "poll-healthcheck.sh"

    root_text = root_entry.read_text(encoding="utf-8")
    assert root_entry.stat().st_mode & 0o111
    assert len(root_text.splitlines()) <= 8
    assert 'exec "$SCRIPT_DIR/scripts/auto-update.sh" "$@"' in root_text
    assert "claude update" not in root_text
    assert "git pull" not in root_text

    canonical_text = canonical.read_text(encoding="utf-8")
    assert canonical.stat().st_mode & 0o111
    assert "git pull" in canonical_text
    assert '"$CLAUDE_BIN" update' in canonical_text
    assert '--upgrade-claude) UPGRADE_CLAUDE=1' in canonical_text
    assert 'if [ "$UPGRADE_CLAUDE" = "1" ]' in canonical_text
    assert "claude-agent-sdk" in canonical_text
    assert "pip install --python" in canonical_text
    assert "lock --upgrade-package" not in canonical_text
    assert "running_launchd_labels()" in canonical_text
    assert 'BABATA_RESTART_LABELS="${BABATA_RESTART_LABELS:-$LABEL_PREFIX}"' in canonical_text
    assert 'grep -Fqx "$label"' in canonical_text
    assert "for label in $LABELS" in canonical_text
    assert '"$SELF_OPS" restart "$label" "$REASON"' in canonical_text
    assert ("launchctl " + "kickstart") not in canonical_text
    assert "wait_runtime_idle()" not in canonical_text
    assert "runtime_file_for_label()" not in canonical_text
    assert "--delay-restart" not in canonical_text
    assert 'if [ "$CLI_CHANGED" = "1" ] || [ "$SDK_CHANGED" = "1" ]; then' in canonical_text
    assert "BABATA_VERSION_WATCH" in canonical_text
    assert "BABATA_VERSION_WATCH_HARD_TIMEOUT_SECONDS" in canonical_text
    assert "run_with_process_group_timeout" in canonical_text
    assert "start_new_session=True" in canonical_text
    assert "os.killpg(process.pid, signal.SIGTERM)" in canonical_text
    assert '--cc-old "${OLD_CLI:-}" --cc-new "${NEW_CLI:-}"' in canonical_text
    assert '--sdk-old "${OLD_SDK:-}" --sdk-new "${NEW_SDK:-}"' in canonical_text
    assert 'echo "WARN: version-watch failed after CLI/SDK update' in canonical_text
    assert 'echo "WARN: version-watch timed out after' in canonical_text

    self_ops_text = self_ops.read_text(encoding="utf-8")
    assert '"$REPO_DIR/scripts/auto-update.sh" --upgrade-claude --upgrade-sdk' in self_ops_text
    assert '"$REPO_DIR/auto-update.sh"' not in self_ops_text
    assert "disable_plist()" in self_ops_text
    assert 'if ! launchctl disable "$domain/$label"; then' in self_ops_text
    assert 'ERROR: failed to disable $domain/$label' in self_ops_text
    assert 'if ! mv "$plist" "$disabled_plist"; then' in self_ops_text
    assert 'ERROR: failed to preserve disabled plist at $disabled_plist' in self_ops_text
    assert "left disabled, not killed" in self_ops_text
    for script in (canonical, self_ops, poll_healthcheck):
        text = script.read_text(encoding="utf-8")
        assert "PROJECT_STATE_DIR=$(grep -m1 '^PROJECT_STATE_DIR='" in text
        assert "tr -d '\\r' || true)" in text

    pyproject_text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert 'addopts = "-p no:cacheprovider"' in pyproject_text


def test_auto_update_kills_hanging_version_watch_tree_and_still_restarts(tmp_path):
    repo = Path(__file__).resolve().parents[1]
    isolated_repo = tmp_path / "babata"
    isolated_scripts = isolated_repo / "scripts"
    isolated_scripts.mkdir(parents=True)

    candidate = isolated_scripts / "auto-update.sh"
    shutil.copy2(repo / "scripts" / "auto-update.sh", candidate)

    def write_executable(path: Path, text: str) -> None:
        path.write_text(text, encoding="utf-8")
        path.chmod(0o755)

    fake_claude = tmp_path / "fake-claude"
    write_executable(
        fake_claude,
        """#!/usr/bin/env bash
set -eu
case "${1:-}" in
  --version)
    if [ -f "$FAKE_CLAUDE_STATE" ]; then
      printf '2.0.0 (fake)\n'
    else
      printf '1.0.0 (fake)\n'
    fi
    ;;
  update)
    : > "$FAKE_CLAUDE_STATE"
    printf 'fake update complete\n'
    ;;
  *) exit 2 ;;
esac
""",
    )

    fake_version_watch = tmp_path / "fake-version-watch"
    write_executable(
        fake_version_watch,
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$$" > "$FAKE_VERSION_WATCH_PARENT_PID"
if [ "${FAKE_VERSION_WATCH_MODE:-hang}" = "fail" ]; then
  exit 23
fi
python3 -c 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)' &
printf '%s\n' "$!" > "$FAKE_VERSION_WATCH_CHILD_PID"
wait
""",
    )

    fake_launchctl = tmp_path / "fake-launchctl"
    write_executable(
        fake_launchctl,
        """#!/usr/bin/env bash
set -eu
[ "${1:-}" = "list" ]
[ "${FAKE_LAUNCHCTL_FAIL:-0}" != "1" ] || exit 44
printf '4321\t0\tcom.babata\n'
printf '4322\t0\tcom.babata.sub2api.watchdog\n'
printf '4323\t0\tcom.babata.podcast\n'
""",
    )

    fake_self_ops_log = tmp_path / "self-ops.log"
    write_executable(
        isolated_scripts / "self-ops.sh",
        """#!/usr/bin/env bash
set -eu
[ "${FAKE_SELF_OPS_FAIL:-0}" != "1" ] || exit 42
printf '%s\n' "$*" >> "$FAKE_SELF_OPS_LOG"
""",
    )

    home = tmp_path / "home"
    home.mkdir()
    parent_pid_file = tmp_path / "version-watch-parent.pid"
    child_pid_file = tmp_path / "version-watch-child.pid"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "CLAUDE_CLI_PATH": str(fake_claude),
            "FAKE_CLAUDE_STATE": str(tmp_path / "claude-updated"),
            "BABATA_VERSION_WATCH": str(fake_version_watch),
            # Leave enough startup margin when this runs inside the full suite;
            # the child itself sleeps for 60s, so 3s still proves hard timeout.
            "BABATA_VERSION_WATCH_HARD_TIMEOUT_SECONDS": "3",
            "FAKE_VERSION_WATCH_PARENT_PID": str(parent_pid_file),
            "FAKE_VERSION_WATCH_CHILD_PID": str(child_pid_file),
            "BABATA_PLATFORM": "Darwin",
            "BABATA_LAUNCHCTL": str(fake_launchctl),
            "FAKE_SELF_OPS_LOG": str(fake_self_ops_log),
        }
    )

    started = time.monotonic()
    result = subprocess.run(
        [str(candidate), "--upgrade-claude"],
        cwd=isolated_repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert elapsed < 10
    log_text = (isolated_repo / "logs" / "auto-update.log").read_text(encoding="utf-8")
    assert (
        "WARN: version-watch timed out after 3s; process tree terminated; "
        "continuing to restart"
    ) in log_text
    assert "launchd restart queued via self-ops: com.babata" in log_text
    assert fake_self_ops_log.read_text(encoding="utf-8").startswith("restart com.babata ")
    assert "sub2api.watchdog" not in fake_self_ops_log.read_text(encoding="utf-8")
    assert "podcast" not in fake_self_ops_log.read_text(encoding="utf-8")

    watched_pids = [
        int(parent_pid_file.read_text(encoding="utf-8")),
        int(child_pid_file.read_text(encoding="utf-8")),
    ]
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if all(not Path(f"/proc/{pid}").exists() for pid in watched_pids) and os.uname().sysname == "Linux":
            break
        alive = []
        for pid in watched_pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            alive.append(pid)
        if not alive:
            break
        time.sleep(0.05)
    for pid in watched_pids:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        raise AssertionError(f"timed-out version-watch process still alive: {pid}")

    # A normal non-zero version-watch result must be distinguished from a hard
    # timeout and must still reach the same restart path.
    Path(env["FAKE_CLAUDE_STATE"]).unlink()
    env["FAKE_VERSION_WATCH_MODE"] = "fail"
    failed_watch = subprocess.run(
        [str(candidate), "--upgrade-claude"],
        cwd=isolated_repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert failed_watch.returncode == 0, failed_watch.stderr
    log_text = (isolated_repo / "logs" / "auto-update.log").read_text(encoding="utf-8")
    assert (
        "WARN: version-watch failed after CLI/SDK update (exit=23); "
        "continuing to restart"
    ) in log_text
    assert fake_self_ops_log.read_text(encoding="utf-8").count("restart com.babata ") == 2

    # Restart dispatch failures are real failures, not optimistic success logs.
    Path(env["FAKE_CLAUDE_STATE"]).unlink()
    env["FAKE_SELF_OPS_FAIL"] = "1"
    failed_restart = subprocess.run(
        [str(candidate), "--upgrade-claude"],
        cwd=isolated_repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert failed_restart.returncode == 1
    assert "ERROR: self-ops restart failed for com.babata" in (
        isolated_repo / "logs" / "auto-update.log"
    ).read_text(encoding="utf-8")

    # Enumeration failure must also fail closed; otherwise a provider/runtime
    # update can leave old channel processes running while reporting success.
    Path(env["FAKE_CLAUDE_STATE"]).unlink()
    env.pop("FAKE_SELF_OPS_FAIL")
    env["FAKE_LAUNCHCTL_FAIL"] = "1"
    failed_list = subprocess.run(
        [str(candidate), "--upgrade-claude"],
        cwd=isolated_repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert failed_list.returncode == 1
    assert "ERROR: cannot enumerate launchd restart targets" in (
        isolated_repo / "logs" / "auto-update.log"
    ).read_text(encoding="utf-8")


def test_pytest_hygiene_removes_local_cache_artifacts():
    repo = Path(__file__).resolve().parents[1]
    conftest_text = (repo / "tests" / "conftest.py").read_text(encoding="utf-8")
    assert "sys.dont_write_bytecode = True" in conftest_text
    assert "def pytest_sessionfinish" in conftest_text
    assert "shutil.rmtree(path, ignore_errors=True)" in conftest_text


def test_test_imports_do_not_mix_venv_python_abis():
    repo = Path(__file__).resolve().parents[1]
    unsafe_probe = 'glob("python' + '*/site-packages")'
    matching_probe = 'f"python{sys.version_info.major}.{sys.version_info.minor}"'

    offenders = []
    for path in sorted((repo / "tests").glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        if unsafe_probe in text:
            offenders.append(path.name)

    assert offenders == []
    assert matching_probe in (repo / "tests" / "conftest.py").read_text(encoding="utf-8")


def test_packaged_modules_cover_runtime_local_imports():
    repo = Path(__file__).resolve().parents[1]
    pyproject = tomllib.loads((repo / "pyproject.toml").read_text(encoding="utf-8"))
    packaged_modules = set(pyproject["tool"]["setuptools"]["py-modules"])
    local_modules = {path.stem for path in repo.glob("*.py")}

    missing_files = sorted(name for name in packaged_modules if not (repo / f"{name}.py").exists())
    assert missing_files == []

    missing_imports: dict[str, list[str]] = {}
    for name in sorted(packaged_modules):
        tree = ast.parse((repo / f"{name}.py").read_text(encoding="utf-8"))
        imported_roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported_roots.add(node.module.split(".")[0])

        missing = sorted((imported_roots & local_modules) - packaged_modules)
        if missing:
            missing_imports[name] = missing

    assert missing_imports == {}


def test_removed_dead_public_helpers_do_not_return():
    repo = Path(__file__).resolve().parents[1]
    forbidden = [
        ("review_health.py", "review_health_status"),
        ("weixin_account.py", "clear_context_tokens"),
        ("weixin_account.py", "load_context_tokens"),
        ("weixin_account.py", "unregister_account"),
        ("weixin_ilink.py", "aes_ecb_padded_size"),
        ("weixin_ilink.py", "decrypt_aes_ecb"),
        ("weixin_ilink.py", "encode_outbound_aes_key"),
        ("weixin_ilink.py", "encrypt_aes_ecb"),
        ("weixin_ilink.py", "is_paused"),
        ("weixin_ilink.py", "parse_inbound_aes_key"),
        ("wizard.py", "read_env"),
        ("cc.py", "mcp_servers_without_repo_bytecode"),
        ("sidebar_events.py", "grep_url"),
        ("sidebar_tool_registry.py", "tool_names"),
        ("blocking_review.py", "blocking_review_enabled"),
        ("media.py", "silk_to_wav"),
        ("media.py", "text_to_silk"),
        ("turn_audit.py", "audit_enabled"),
        ("turn_audit.py", "declared_checks_enabled"),
        ("turn_audit.py", "guard_mode"),
        ("turn_audit.py", "review_bus_mode"),
        ("memory_runtime.py", "memory_inject_script"),
        ("memory_runtime.py", "memory_reflex_script"),
        ("memory_runtime.py", "memory_reflex_timeout"),
        ("memory_runtime.py", "format_memory_reflex_hint"),
        ("memory_runtime.py", "memory_reflex_enabled"),
        ("memory_runtime.py", "memory_reflex_for_prompt"),
        ("memory_runtime.py", "log_memory_reflex_preflight"),
        ("weixin_bot.py", "strip_markdown"),
        ("weixin_bot.py", "chunk_text"),
    ]

    for filename, name in forbidden:
        tree = ast.parse((repo / filename).read_text(encoding="utf-8"))
        definitions = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        assert name not in definitions


def test_sidebar_modules_have_no_import_time_write_probe():
    repo = Path(__file__).resolve().parents[1]
    for filename in ("sidebar_history.py", "sidebar_events.py", "sidebar_translate.py"):
        text = (repo / filename).read_text(encoding="utf-8")
        tree = ast.parse(text)
        top_level_calls = [
            node
            for node in tree.body
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call)
        ]
        assert "_probe_persistence" not in text
        assert ".history_probe" not in text
        assert not top_level_calls


def test_review_cc_worker_candidates_are_not_duplicated():
    repo = Path(__file__).resolve().parents[1]
    duplicate = 'Path("~/cc-workspace/bin/cc-worker").expanduser()'
    offenders = [
        path.name
        for path in (repo / "review_health.py", repo / "blocking_review.py")
        if duplicate in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_sidebar_history_route_is_get_only():
    text = (Path(__file__).resolve().parents[1] / "sidebar_bot.py").read_text(encoding="utf-8")

    assert 'web.get("/history", handle_history)' in text
    assert 'web.post("/history", handle_history)' not in text


def test_sidebar_translate_private_helpers_have_call_sites():
    path = Path(__file__).resolve().parents[1] / "sidebar_translate.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))

    private_functions = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("_")
        and not (node.name.startswith("__") and node.name.endswith("__"))
    ]
    used_names = [
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    ]

    assert [name for name in private_functions if name not in used_names] == []


def test_python_sources_avoid_private_user_shorthand():
    repo = Path(__file__).resolve().parents[1]
    files = sorted(repo.glob("*.py")) + sorted((repo / "tests").glob("test_*.py"))
    private_shorthand = re.compile(r"(?<![A-Z_])V(?:'s| [A-Za-z\u4e00-\u9fff])")

    hits: list[str] = []
    for path in files:
        text = path.read_text(encoding="utf-8")
        if path.name == "test_runtime_hygiene.py":
            text = "\n".join(
                line
                for line in text.splitlines()
                if "private_shorthand = re.compile" not in line
            )
        for match in private_shorthand.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            hits.append(f"{path.relative_to(repo)}:{line}:{match.group(0)}")

    assert hits == []
