import json
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

import memory_health


def _issue_buckets():
    return memory_health._empty_issue_buckets()


def test_scan_root_memory_files_keeps_root_checks_together(tmp_path):
    (tmp_path / "MEMORY.md").write_text("# memory\n", encoding="utf-8")
    (tmp_path / "good.md").write_text(
        "---\nname: Good\ndescription: ok\ntype: project\n---\nbody\n",
        encoding="utf-8",
    )
    (tmp_path / "bad.md").write_text(
        "---\nname: Bad\ntype: unknown\n---\n",
        encoding="utf-8",
    )
    issues = _issue_buckets()

    orphans = memory_health._scan_root_memory_files(tmp_path, {"good.md"}, issues)

    assert orphans == [
        {
            "file": "bad.md",
            "line": 0,
            "detail": "exists but is not linked from MEMORY.md or indexes/*.md",
        }
    ]
    assert issues["orphan"] == orphans
    assert issues["missing_field"] == [
        {
            "file": "bad.md",
            "line": 0,
            "detail": "frontmatter missing required field 'description'",
        }
    ]
    assert issues["invalid_type"][0]["file"] == "bad.md"
    assert "invalid type 'unknown'" in issues["invalid_type"][0]["detail"]
    assert issues["empty_body"] == [
        {"file": "bad.md", "line": 0, "detail": "no content after frontmatter"}
    ]
    assert issues["too_long"] == []


def test_scan_category_indexes_updates_indexed_and_count_mismatch(tmp_path):
    indexes = tmp_path / "indexes"
    indexes.mkdir()
    (tmp_path / "good.md").write_text("# good\n", encoding="utf-8")
    (indexes / "01-main.md").write_text(
        "- [good](../good.md)\n- [missing](../missing.md)\n",
        encoding="utf-8",
    )
    issues = _issue_buckets()
    indexed: set[str] = set()

    memory_health._scan_category_indexes(
        tmp_path,
        {"01-main.md": (2, 7)},
        indexed,
        issues,
    )

    assert indexed == {"good.md"}
    assert issues["broken_link"] == [
        {
            "file": "indexes/01-main.md",
            "line": 2,
            "detail": "points to missing '../missing.md'",
        }
    ]
    assert issues["count_mismatch"] == [
        {
            "file": "MEMORY.md",
            "line": 7,
            "detail": "01-main.md declares 2 条 but index has 1",
        }
    ]


def test_run_human_mode_accepts_clean_router(tmp_path, capsys):
    (tmp_path / "MEMORY.md").write_text("- [good](good.md)\n", encoding="utf-8")
    (tmp_path / "good.md").write_text("# good\n", encoding="utf-8")

    code = memory_health.run(
        tmp_path, json_mode=False, fix_mode=False, strict_mode=True
    )

    assert code == 0
    assert capsys.readouterr().out == "No structural issues found.\n"


def test_run_json_strict_reports_router_and_root_issues(tmp_path, capsys):
    (tmp_path / "MEMORY.md").write_text("- [missing](missing.md)\n", encoding="utf-8")
    (tmp_path / "orphan.md").write_text("# orphan\n", encoding="utf-8")

    code = memory_health.run(
        tmp_path, json_mode=True, fix_mode=False, strict_mode=True
    )
    payload = json.loads(capsys.readouterr().out)

    assert code == 1
    assert payload == {
        "broken_link": [
            {
                "file": "MEMORY.md",
                "line": 1,
                "detail": "points to missing 'missing.md'",
            }
        ],
        "orphan": [
            {
                "file": "orphan.md",
                "line": 0,
                "detail": "exists but is not linked from MEMORY.md or indexes/*.md",
            }
        ],
    }
