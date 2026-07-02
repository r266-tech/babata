import ast
from pathlib import Path


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
