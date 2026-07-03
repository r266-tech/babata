import ast
from pathlib import Path


def test_auto_update_has_single_canonical_implementation():
    repo = Path(__file__).resolve().parents[1]
    root_entry = repo / "auto-update.sh"
    canonical = repo / "scripts" / "auto-update.sh"
    self_ops = repo / "scripts" / "self-ops.sh"

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

    self_ops_text = self_ops.read_text(encoding="utf-8")
    assert '"$REPO_DIR/scripts/auto-update.sh" --upgrade-sdk' in self_ops_text
    assert '"$REPO_DIR/auto-update.sh"' not in self_ops_text


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
