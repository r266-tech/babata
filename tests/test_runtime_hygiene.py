import ast
import re
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
    assert 'exec "$SCRIPT_DIR/scripts/auto-update.sh" --upgrade-sdk "$@"' in root_text
    assert "claude update" not in root_text
    assert "git pull" not in root_text

    canonical_text = canonical.read_text(encoding="utf-8")
    assert canonical.stat().st_mode & 0o111
    assert "git pull" in canonical_text
    assert "claude update" in canonical_text
    assert "claude-agent-sdk" in canonical_text
    assert "running_launchd_labels()" in canonical_text
    assert "launchctl list" in canonical_text
    assert "for label in $LABELS" in canonical_text
    assert '"$SELF_OPS" restart "$label" "$REASON"' in canonical_text
    assert ("launchctl " + "kickstart") not in canonical_text
    assert "wait_runtime_idle()" not in canonical_text
    assert "runtime_file_for_label()" not in canonical_text
    assert "--delay-restart" not in canonical_text

    self_ops_text = self_ops.read_text(encoding="utf-8")
    assert '"$REPO_DIR/scripts/auto-update.sh" --upgrade-sdk' in self_ops_text
    assert '"$REPO_DIR/auto-update.sh"' not in self_ops_text
    for script in (canonical, self_ops, poll_healthcheck):
        text = script.read_text(encoding="utf-8")
        assert "PROJECT_STATE_DIR=$(grep -m1 '^PROJECT_STATE_DIR='" in text
        assert "tr -d '\\r' || true)" in text

    pyproject_text = (repo / "pyproject.toml").read_text(encoding="utf-8")
    assert 'addopts = "-p no:cacheprovider"' in pyproject_text


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
