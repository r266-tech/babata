#!/usr/bin/env python3
"""memory_health.py — structural health scanner for babata memory directory."""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

# Threshold: memories should stay concise; 500 lines signals drift / bloat.
LENGTH_THRESHOLD_LINES = 500
# Legal types for frontmatter.
LEGAL_TYPES = {"user", "feedback", "project", "reference"}


def parse_frontmatter(text: str) -> dict[str, str] | None:
    """Thin YAML frontmatter parser; no external deps."""
    if not text.startswith("---"):
        return None
    end = text.find("\n---", 3)
    if end == -1:
        return None
    raw = text[3:end].strip()
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        result[key.strip()] = val.strip()
    return result


def extract_body(text: str) -> str:
    """Return text after the closing frontmatter delimiter."""
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    start = end + 4
    if start < len(text) and text[start] == "\n":
        start += 1
    return text[start:]


def human_report(issues: dict[str, list[dict[str, Any]]]) -> None:
    """Pretty print grouped issues to stdout."""
    if not any(issues.values()):
        print("No structural issues found.")
        return
    for kind, items in issues.items():
        if not items:
            continue
        print(f"\n[{kind}]")
        for item in items:
            line = item.get("line", 0)
            line_str = f":{line}" if line else ""
            print(f"  {item['file']}{line_str}  {item['detail']}")


def json_report(issues: dict[str, list[dict[str, Any]]]) -> None:
    print(json.dumps(issues, indent=2, ensure_ascii=False))


def fix_orphans(root: Path, orphans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Append orphan entries to MEMORY.md tail. Safe because we only touch the index."""
    memory_md = root / "MEMORY.md"
    if not memory_md.exists():
        return [*orphans, {"file": "MEMORY.md", "line": 0, "detail": "MEMORY.md missing; cannot fix orphans"}]
    lines_to_add: list[str] = []
    for o in orphans:
        filename = o["file"]
        p = root / filename
        title = filename
        try:
            fm = parse_frontmatter(p.read_text())
            if fm and "name" in fm:
                title = fm["name"]
        except Exception:
            pass
        lines_to_add.append(f"- [{title}]({filename}) — auto-indexed by memory_health")
    if lines_to_add:
        content = memory_md.read_text()
        if not content.endswith("\n"):
            content += "\n"
        content += "\n".join(lines_to_add) + "\n"
        memory_md.write_text(content)
    return []


def run(root: Path, *, json_mode: bool, fix_mode: bool, strict_mode: bool) -> int:
    issues: dict[str, list[dict[str, Any]]] = {
        "broken_link": [],
        "orphan": [],
        "duplicate_index": [],
        "cross_dir_link": [],
        "missing_frontmatter": [],
        "missing_field": [],
        "invalid_type": [],
        "empty_body": [],
        "too_long": [],
    }

    # ---- Parse MEMORY.md ----
    memory_md = root / "MEMORY.md"
    indexed: set[str] = set()
    seen_files: dict[str, int] = {}
    entry_pattern = re.compile(r"^-\s*\[(.*?)\]\((.*?)\)\s*—\s*(.*)$")

    if memory_md.exists():
        for line_no, raw in enumerate(memory_md.read_text().splitlines(), 1):
            raw = raw.strip()
            if not raw.startswith("-"):
                continue
            m = entry_pattern.match(raw)
            if not m:
                continue
            _, filename, _ = m.groups()
            filename = filename.strip()

            # Extra check: root index should never reference subdirs (daily/archive/rollup
            # maintain their own aggregation; cross-directory links fracture hierarchy).
            if "/" in filename:
                issues["cross_dir_link"].append(
                    {"file": "MEMORY.md", "line": line_no, "detail": f"references subdirectory file '{filename}'"}
                )
                continue

            # Check 1: points to non-existent file
            fpath = root / filename
            if not fpath.exists():
                issues["broken_link"].append(
                    {"file": "MEMORY.md", "line": line_no, "detail": f"points to missing '{filename}'"}
                )
                continue

            # Check 3: same file indexed more than once
            if filename in seen_files:
                issues["duplicate_index"].append(
                    {"file": "MEMORY.md", "line": line_no, "detail": f"duplicate of line {seen_files[filename]} for '{filename}'"}
                )
            else:
                seen_files[filename] = line_no

            indexed.add(filename)
    else:
        issues["broken_link"].append(
            {"file": "MEMORY.md", "line": 0, "detail": "MEMORY.md does not exist"}
        )

    # ---- Scan root-level memory files ----
    root_md_files: list[Path] = []
    for p in root.iterdir():
        if p.is_file() and p.suffix == ".md" and p.name != "MEMORY.md":
            root_md_files.append(p)

    # Check 2: root files not indexed in MEMORY.md (daily/archive/rollup excluded by design)
    orphans: list[dict[str, Any]] = []
    for p in root_md_files:
        if p.name not in indexed:
            orphans.append({"file": p.name, "line": 0, "detail": f"exists but not indexed in MEMORY.md"})
            issues["orphan"].append(orphans[-1])

    # ---- Per-file checks ----
    for p in root_md_files:
        text = p.read_text()
        lines = text.splitlines()

        fm = parse_frontmatter(text)
        if fm is None:
            issues["missing_frontmatter"].append(
                {"file": p.name, "line": 0, "detail": "missing or malformed YAML frontmatter"}
            )
        else:
            # Check 4: required fields + type enum
            for field in ("name", "description", "type"):
                if field not in fm or not fm[field]:
                    issues["missing_field"].append(
                        {"file": p.name, "line": 0, "detail": f"frontmatter missing required field '{field}'"}
                    )
            if "type" in fm and fm["type"] not in LEGAL_TYPES:
                issues["invalid_type"].append(
                    {"file": p.name, "line": 0, "detail": f"invalid type '{fm['type']}' (legal: {', '.join(sorted(LEGAL_TYPES))})"}
                )

            # Extra check: empty body after frontmatter signals placeholder / incomplete note.
            body = extract_body(text)
            if not body.strip():
                issues["empty_body"].append(
                    {"file": p.name, "line": 0, "detail": "no content after frontmatter"}
                )

        # Check 5: excessive length
        if len(lines) > LENGTH_THRESHOLD_LINES:
            issues["too_long"].append(
                {"file": p.name, "line": 0, "detail": f"{len(lines)} lines > threshold {LENGTH_THRESHOLD_LINES}"}
            )

    # ---- Fix mode ----
    if fix_mode and orphans:
        new_issues = fix_orphans(root, orphans)
        for ni in new_issues:
            issues.setdefault("fix_failure", []).append(ni)

    # ---- Output ----
    if json_mode:
        # Strip empty groups for brevity
        json_issues = {k: v for k, v in issues.items() if v}
        json_report(json_issues)
    else:
        human_report(issues)

    total = sum(len(v) for v in issues.values())
    if strict_mode and total > 0:
        return 1
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Structural health scanner for babata memory directory")
    parser.add_argument("--root", required=True, help="Path to memory directory")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON")
    parser.add_argument("--fix", action="store_true", help="Fix safe issues (append orphans to MEMORY.md)")
    parser.add_argument("--strict", action="store_true", help="Exit non-zero if any issue found")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.is_dir():
        print(f"error: --root '{root}' is not a directory", file=sys.stderr)
        sys.exit(2)

    code = run(root, json_mode=args.json, fix_mode=args.fix, strict_mode=args.strict)
    sys.exit(code)


if __name__ == "__main__":
    main()
