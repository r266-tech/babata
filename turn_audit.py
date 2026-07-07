"""babata-native turn ledger, deterministic guards, checks, and optional review queue.

The audit loop is deliberately transport-neutral. Channels and CPUs keep their
existing isolation; this module only records facts, runs deterministic local
policy checks, executes repo-declared checks when present, and optionally emits
review-bus records when explicitly enabled.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from constants import NAMESPACE, STATE_DIR


_MAX_PROMPT_PREVIEW = 160
_MAX_FINAL_PREVIEW = 240
_MAX_ERROR_PREVIEW = 240
_MAX_COMMAND_PREVIEW = 500
_MAX_CHECK_OUTPUT = 4000
_MAX_FILE_BYTES = 512 * 1024
_DEFAULT_CHECK_TIMEOUT_SECONDS = 180

_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("anthropic_api_key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b")),
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("github_token", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("telegram_bot_token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b")),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    (
        "secret_assignment",
        re.compile(
            r"(?i)\b(?:OPENAI_API_KEY|ANTHROPIC_API_KEY|CLAUDE_API_KEY|TG_TOKEN|"
            r"TELEGRAM_TOKEN|WECHAT_TOKEN|AUTH_TOKEN|COOKIE|CT0)\s*="
        ),
    ),
)

_TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".css",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".jsonl",
    ".md",
    ".mjs",
    ".plist",
    ".py",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".txt",
    ".yaml",
    ".yml",
}


@dataclass
class TurnAudit:
    turn_id: str
    cpu: str
    channel: str
    cwd: Path
    repo_root: Path | None
    started_at: float
    prompt: str
    session_id_before: str | None = None
    baseline_head: str | None = None
    baseline_status_hash: str | None = None
    baseline_status_entries: dict[str, str] = field(default_factory=dict)
    baseline_dirty_fingerprints: dict[str, str] = field(default_factory=dict)
    baseline_sensitive_files: dict[str, str] = field(default_factory=dict)
    checks_config_rel: str | None = None
    checks_config_fingerprint: str | None = None
    images_count: int = 0
    record: dict[str, Any] = field(default_factory=dict)


def audit_enabled() -> bool:
    return os.environ.get("BABATA_TURN_LEDGER", "1") != "0"


def guard_mode() -> str:
    mode = os.environ.get("BABATA_DETERMINISTIC_GUARDS", "observe").strip().lower()
    if mode in {"0", "off", "false", "disabled"}:
        return "off"
    if mode in {"enforce", "block"}:
        return "enforce"
    return "observe"


def declared_checks_enabled() -> bool:
    return os.environ.get("BABATA_DECLARED_CHECKS", "1") != "0"


def review_bus_mode() -> str:
    mode = os.environ.get("BABATA_REVIEW_BUS", "off").strip().lower()
    if mode in {"0", "off", "false", "disabled"}:
        return "off"
    if mode in {"queue", "dry-run", "dry_run", "enqueue"}:
        return "queue"
    if mode in {"worker", "enforce"}:
        return mode
    return "off"


def begin_turn(
    *,
    cpu: str,
    channel: str,
    prompt: str,
    session_id_before: str | None,
    cwd: str | Path | None = None,
    images_count: int = 0,
) -> TurnAudit | None:
    """Start a turn audit and append a begin record.

    Disabled audit is a true no-op, so tests and special runtimes can opt out by
    setting BABATA_TURN_LEDGER=0.
    """
    if not audit_enabled():
        return None

    actual_cwd = Path(cwd or os.getcwd()).expanduser().resolve()
    repo_root = _git_root(actual_cwd)
    baseline_head = _git_output(repo_root, "rev-parse", "HEAD") if repo_root else None
    baseline_status = _git_status_z(repo_root) if repo_root else ""
    baseline_status_entries = _status_entries(baseline_status)
    baseline_dirty_fingerprints = _fingerprints_for(repo_root, baseline_status_entries)
    baseline_sensitive_files = _sensitive_file_fingerprints(repo_root)
    checks_config = _checks_config_path(repo_root) if repo_root else None
    checks_config_rel = checks_config.relative_to(repo_root).as_posix() if repo_root and checks_config else None
    checks_config_fingerprint = _file_fingerprint(checks_config) if checks_config else None
    started_at = time.time()
    turn = TurnAudit(
        turn_id=f"{int(started_at * 1000)}-{uuid.uuid4().hex[:10]}",
        cpu=cpu,
        channel=channel,
        cwd=actual_cwd,
        repo_root=repo_root,
        started_at=started_at,
        prompt=prompt,
        session_id_before=session_id_before,
        baseline_head=baseline_head,
        baseline_status_hash=_sha256_text(baseline_status) if baseline_status else None,
        baseline_status_entries=baseline_status_entries,
        baseline_dirty_fingerprints=baseline_dirty_fingerprints,
        baseline_sensitive_files=baseline_sensitive_files,
        checks_config_rel=checks_config_rel,
        checks_config_fingerprint=checks_config_fingerprint,
        images_count=images_count,
    )
    turn.record = {
        "schema": "babata.turn_audit.v1",
        "turn_id": turn.turn_id,
        "event": "begin",
        "started_at": _iso(started_at),
        "cpu": cpu,
        "channel": channel,
        "cwd": str(actual_cwd),
        "repo_root": str(repo_root) if repo_root else None,
        "session_id_before": session_id_before,
        "git": {
            "baseline_head": baseline_head,
            "baseline_status_hash": turn.baseline_status_hash,
            "baseline_dirty_files": sorted(baseline_status_entries),
            "baseline_sensitive_files": sorted(baseline_sensitive_files),
        },
        "declared_checks_config": checks_config_rel,
        "prompt_preview": _preview(prompt, _MAX_PROMPT_PREVIEW),
        "prompt_sha256": _sha256_text(prompt),
        "prompt_bytes": _text_bytes(prompt),
        "images_count": images_count,
    }
    _append_jsonl(_ledger_path(), turn.record)
    return turn


def finish_turn(
    turn: TurnAudit | None,
    *,
    response: Any | None = None,
    error: BaseException | None = None,
    tools: list[str] | None = None,
    tool_uses: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    if turn is None:
        return None

    ended_at = time.time()
    changed_files = _changed_files_since_begin(turn)
    final_content = str(getattr(response, "content", "") or "")
    session_id_after = getattr(response, "session_id", None) if response is not None else None
    tool_uses = list(tool_uses or [])
    tool_names = _ordered_unique(list(tools or []) + [
        str(t.get("name")) for t in tool_uses if t.get("name")
    ])
    guard_findings = run_deterministic_guards(
        repo_root=turn.repo_root,
        changed_files=changed_files,
        tool_uses=tool_uses,
    )
    check_results = run_declared_checks(
        repo_root=turn.repo_root,
        changed_files=changed_files,
        guard_findings=guard_findings,
        baseline_config_rel=turn.checks_config_rel,
        baseline_config_fingerprint=turn.checks_config_fingerprint,
        require_baseline_config=True,
    )
    review_tasks = enqueue_review_tasks(
        turn=turn,
        changed_files=changed_files,
        guard_findings=guard_findings,
        check_results=check_results,
        tool_uses=tool_uses,
    )
    record = {
        **turn.record,
        "event": "finish",
        "ended_at": _iso(ended_at),
        "duration_ms": round((ended_at - turn.started_at) * 1000),
        "session_id_after": session_id_after,
        "git": {
            **(turn.record.get("git") or {}),
            "head_after": _git_output(turn.repo_root, "rev-parse", "HEAD") if turn.repo_root else None,
            "changed_files": changed_files,
        },
        "tools": tool_names,
        "tool_uses": _compact_tool_uses(tool_uses),
        "guard_mode": guard_mode(),
        "guard_findings": guard_findings,
        "declared_checks": check_results,
        "review_bus_mode": review_bus_mode(),
        "review_tasks": review_tasks,
        "final_preview": _preview(final_content, _MAX_FINAL_PREVIEW),
        "final_sha256": _sha256_text(final_content),
        "final_bytes": _text_bytes(final_content),
        "error": _error_record(error),
    }
    _append_jsonl(_ledger_path(), record)
    return _public_summary(record)


def run_deterministic_guards(
    *,
    repo_root: Path | None,
    changed_files: list[str],
    tool_uses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    mode = guard_mode()
    if mode == "off":
        return []

    findings: list[dict[str, Any]] = []
    for rel in changed_files:
        findings.extend(_changed_file_guard_findings(repo_root, rel))

    for tool in tool_uses:
        findings.extend(_tool_guard_findings(tool))

    return _dedupe_findings(findings)


def _changed_file_guard_findings(repo_root: Path | None, rel: str) -> list[dict[str, Any]]:
    rel_posix = rel.replace("\\", "/")
    name = Path(rel_posix).name
    lower = rel_posix.lower()
    findings: list[dict[str, Any]] = []

    if name == ".env" or name.startswith(".env."):
        findings.append(_finding(
            "high",
            "env-file-changed",
            "Environment/secret file changed; keep local secrets out of repo commits and public artifacts.",
            path=rel,
            blocking=True,
        ))
    if "chat-archive" in lower or ("/raw/" in lower and "archive" in lower):
        findings.append(_finding(
            "medium",
            "raw-archive-change",
            "Raw/archive layer changed; babata raw records are append-only unless explicitly authorized.",
            path=rel,
        ))
    if _is_ops_path(rel_posix):
        findings.append(_finding(
            "medium",
            "ops-boundary-file",
            "Launchd/self-ops surface changed; verify rollout uses scripts/self-ops.sh instead of inline service mutation.",
            path=rel,
        ))

    if repo_root is None:
        return findings
    fp = (repo_root / rel).resolve()
    if not _safe_child(repo_root, fp) or not fp.is_file() or not _looks_text(fp):
        return findings
    content = _read_small_text(fp)
    if not content:
        return findings
    for rule, pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            findings.append(_finding(
                "high",
                f"secret-pattern:{rule}",
                "Changed file contains a token-like secret pattern.",
                path=rel,
                blocking=True,
            ))
    if "launchctl kickstart" in content or "launchctl bootstrap" in content or "launchctl bootout" in content:
        if rel_posix != "scripts/self-ops.sh":
            findings.append(_finding(
                "high",
                "inline-launchctl",
                "Service mutation must go through scripts/self-ops.sh for detached self-ops safety.",
                path=rel,
                blocking=True,
            ))
    return findings


def _tool_guard_findings(tool: dict[str, Any]) -> list[dict[str, Any]]:
    name = str(tool.get("name") or "")
    path = _tool_path(tool)
    findings = _tool_path_guard_findings(path)
    findings.extend(_tool_content_guard_findings(tool, path))

    command = _tool_command(tool)
    if not command:
        return findings
    if name.lower() != "bash" and "bash" not in name.lower() and "command" not in tool:
        return findings
    findings.extend(_tool_command_guard_findings(command))
    return findings


def _tool_path_guard_findings(path: str | None) -> list[dict[str, Any]]:
    if not path:
        return []
    path_name = Path(path.replace("\\", "/")).name
    findings: list[dict[str, Any]] = []
    if path_name == ".env" or path_name.startswith(".env."):
        findings.append(_finding(
            "high",
            "env-file-tool-request",
            "Tool request targets an environment/secret file.",
            path=path,
            blocking=True,
        ))
    if _is_ops_path(path):
        findings.append(_finding(
            "medium",
            "ops-boundary-tool-request",
            "Tool request targets launchd/self-ops surface.",
            path=path,
        ))
    return findings


def _tool_content_guard_findings(
    tool: dict[str, Any],
    path: str | None,
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if tool.get("content_has_secret"):
        findings.append(_finding(
            "high",
            "secret-pattern:tool-input",
            "Tool input contains a token-like secret pattern.",
            path=path,
            blocking=True,
        ))
    if tool.get("content_has_launchctl") and path != "scripts/self-ops.sh":
        findings.append(_finding(
            "high",
            "inline-launchctl-tool-input",
            "Tool input contains launchd mutation outside scripts/self-ops.sh.",
            path=path,
            blocking=True,
        ))
    return findings


def _tool_command_guard_findings(command: str) -> list[dict[str, Any]]:
    lowered = command.lower()
    findings: list[dict[str, Any]] = []
    if _is_dangerous_git_command(command):
        findings.append(_finding(
            "high",
            "dangerous-git-command",
            "Dangerous git command was requested; only run with explicit user authorization.",
            command=command,
            blocking=True,
        ))
    if "launchctl kickstart" in lowered or "launchctl bootstrap" in lowered or "launchctl bootout" in lowered:
        if "scripts/self-ops.sh" not in lowered:
            findings.append(_finding(
                "high",
                "inline-launchctl-command",
                "Launchd mutation command bypassed scripts/self-ops.sh.",
                command=command,
                blocking=True,
            ))
    if re.search(r"\b(?:rm\s+-rf|trash\s+).*(?:/state/|chat-archive|memory)\b", lowered):
        findings.append(_finding(
            "high",
            "destructive-memory-command",
            "Command appears to delete state/memory/archive data.",
            command=command,
            blocking=True,
        ))
    return findings


def _is_dangerous_git_command(command: str) -> bool:
    return bool(
        re.search(r"\bgit\s+(?:reset\s+--hard|checkout\s+--)\b", command)
        or re.search(r"\bgit\s+clean\s+-[A-Za-z]*[fd][A-Za-z]*\b", command)
    )


def run_declared_checks(
    *,
    repo_root: Path | None,
    changed_files: list[str],
    guard_findings: list[dict[str, Any]],
    baseline_config_rel: str | None = None,
    baseline_config_fingerprint: str | None = None,
    require_baseline_config: bool = False,
) -> list[dict[str, Any]]:
    checks, early_result = _declared_check_items(
        repo_root=repo_root,
        baseline_config_rel=baseline_config_rel,
        baseline_config_fingerprint=baseline_config_fingerprint,
        require_baseline_config=require_baseline_config,
    )
    if early_result is not None:
        return early_result

    context = _check_context(changed_files, guard_findings)
    results = [
        _run_declared_check_item(repo_root, item, idx, context)
        for idx, item in enumerate(checks)
    ]
    return results or [{"status": "skipped", "reason": "no checks declared"}]


def _declared_check_items(
    *,
    repo_root: Path | None,
    baseline_config_rel: str | None,
    baseline_config_fingerprint: str | None,
    require_baseline_config: bool,
) -> tuple[list[Any], list[dict[str, Any]] | None]:
    if not declared_checks_enabled():
        return [], [{"status": "skipped", "reason": "BABATA_DECLARED_CHECKS=0"}]
    if repo_root is None:
        return [], [{"status": "skipped", "reason": "not a git repo"}]

    config_path = _checks_config_path(repo_root)
    if config_path is None:
        return [], [{"status": "skipped", "reason": "no .babata/checks.json"}]
    if require_baseline_config and (baseline_config_rel is None or baseline_config_fingerprint is None):
        return [], [{"status": "skipped", "reason": "declared checks config changed during turn"}]
    if require_baseline_config or baseline_config_rel is not None or baseline_config_fingerprint is not None:
        current_rel = config_path.relative_to(repo_root).as_posix()
        current_fingerprint = _file_fingerprint(config_path)
        if baseline_config_rel != current_rel:
            return [], [{"status": "skipped", "reason": "declared checks config changed during turn"}]
        if baseline_config_fingerprint != current_fingerprint:
            return [], [{"status": "skipped", "reason": "declared checks config changed during turn"}]

    try:
        data = json.loads(config_path.read_text())
    except Exception as e:
        return [], [{"status": "config_error", "path": str(config_path), "error": str(e)}]

    checks = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(checks, list):
        return [], [{"status": "config_error", "path": str(config_path), "error": "checks must be a list"}]
    return checks, None


def _run_declared_check_item(
    repo_root: Path,
    item: Any,
    idx: int,
    context: set[str],
) -> dict[str, Any]:
    if not isinstance(item, dict):
        return {"status": "config_error", "index": idx, "error": "check must be an object"}

    name = str(item.get("name") or f"check-{idx + 1}")
    command = item.get("command")
    if not isinstance(command, str) or not command.strip():
        return {"name": name, "status": "config_error", "error": "command is required"}
    when = item.get("when")
    if when is not None and not _check_matches(when, context):
        return {"name": name, "status": "skipped", "reason": "when did not match"}

    timeout = _check_timeout(item.get("timeout_seconds"))
    started = time.time()
    try:
        proc = subprocess.run(
            command,
            cwd=repo_root,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
        return {
            "name": name,
            "command": command,
            "status": "passed" if proc.returncode == 0 else "failed",
            "exit_code": proc.returncode,
            "duration_ms": round((time.time() - started) * 1000),
            "output_tail": _tail(output, _MAX_CHECK_OUTPUT),
        }
    except subprocess.TimeoutExpired as e:
        output = "\n".join(
            part.decode("utf-8", errors="replace") if isinstance(part, bytes) else str(part)
            for part in (e.stdout, e.stderr)
            if part
        )
        return {
            "name": name,
            "command": command,
            "status": "timeout",
            "duration_ms": round((time.time() - started) * 1000),
            "timeout_seconds": timeout,
            "output_tail": _tail(output, _MAX_CHECK_OUTPUT),
        }
    except Exception as e:
        return {
            "name": name,
            "command": command,
            "status": "error",
            "duration_ms": round((time.time() - started) * 1000),
            "error": str(e),
        }


def enqueue_review_tasks(
    *,
    turn: TurnAudit,
    changed_files: list[str],
    guard_findings: list[dict[str, Any]],
    check_results: list[dict[str, Any]],
    tool_uses: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    mode = review_bus_mode()
    if mode == "off":
        return []

    kinds = _review_kinds(changed_files, guard_findings, check_results)
    if not kinds:
        return []

    tasks: list[dict[str, Any]] = []
    for kind in kinds:
        task = {
            "schema": "babata.review_task.v1",
            "task_id": f"{turn.turn_id}-{kind}",
            "turn_id": turn.turn_id,
            "created_at": _iso(time.time()),
            "status": "queued",
            "mode": mode,
            "kind": kind,
            "source_cpu": turn.cpu,
            "reviewer_cpu": _reviewer_for(turn.cpu, kind),
            "channel": turn.channel,
            "repo_root": str(turn.repo_root) if turn.repo_root else None,
            "baseline_head": turn.baseline_head,
            "changed_files": changed_files,
            "guard_findings": guard_findings,
            "declared_checks": check_results,
            "tool_uses": _compact_tool_uses(tool_uses),
        }
        _append_jsonl(_review_bus_path(), task)
        tasks.append({
            "task_id": task["task_id"],
            "kind": kind,
            "status": "queued",
            "reviewer_cpu": task["reviewer_cpu"],
        })
    return tasks


def summarize_tool_use(name: str, tool_input: dict[str, Any] | None) -> dict[str, Any]:
    out: dict[str, Any] = {"name": name}
    tool_input = tool_input or {}
    command = tool_input.get("command") or tool_input.get("cmd")
    if isinstance(command, str):
        out["command"] = _preview(command, _MAX_COMMAND_PREVIEW)
    for key in ("file_path", "path", "notebook_path"):
        value = tool_input.get(key)
        if isinstance(value, str):
            out[key] = value
            break
    content_text = _tool_content_text(tool_input)
    if content_text:
        if any(pattern.search(content_text) for _, pattern in _SECRET_PATTERNS):
            out["content_has_secret"] = True
        lowered = content_text.lower()
        if "launchctl kickstart" in lowered or "launchctl bootstrap" in lowered or "launchctl bootout" in lowered:
            out["content_has_launchctl"] = True
    if name in {"Edit", "Write", "MultiEdit", "NotebookEdit"}:
        out["mutates_files"] = True
    return out


def should_block_for_permission(tool_name: str, tool_input: dict[str, Any] | None) -> tuple[bool, str | None]:
    if guard_mode() != "enforce":
        return False, None
    findings = run_deterministic_guards(
        repo_root=None,
        changed_files=[],
        tool_uses=[summarize_tool_use(tool_name, tool_input)],
    )
    blocking = [f for f in findings if f.get("blocking")]
    if not blocking:
        return False, None
    reason = "; ".join(f"{f.get('rule')}: {f.get('message')}" for f in blocking[:3])
    return True, reason


def _ledger_path() -> Path:
    return _audit_dir() / f"{NAMESPACE}-turn-ledger.jsonl"


def _review_bus_path() -> Path:
    return _audit_dir() / f"{NAMESPACE}-review-bus.jsonl"


def _audit_dir() -> Path:
    path = Path(os.environ.get("BABATA_AUDIT_DIR", str(STATE_DIR / "audit"))).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        # Audit must never take down a user-facing channel.
        return


def _git_root(cwd: Path) -> Path | None:
    root = _git_output(cwd, "rev-parse", "--show-toplevel")
    return Path(root).resolve() if root else None


def _git_output(cwd: Path | None, *args: str) -> str | None:
    if cwd is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            text=True,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def _git_status_z(repo_root: Path | None) -> str:
    if repo_root is None:
        return ""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain=v1", "-z"],
            text=True,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _changed_files_since_begin(turn: TurnAudit) -> list[str]:
    repo_root = turn.repo_root
    if repo_root is None:
        return []

    files: set[str] = set()
    current_status = _status_entries(_git_status_z(repo_root))
    current_fingerprints = _fingerprints_for(repo_root, current_status)
    all_status_paths = set(turn.baseline_status_entries) | set(current_status)
    for rel in all_status_paths:
        if turn.baseline_status_entries.get(rel) != current_status.get(rel):
            files.add(rel)
            continue
        if turn.baseline_dirty_fingerprints.get(rel) != current_fingerprints.get(rel):
            files.add(rel)

    if turn.baseline_head:
        head_after = _git_output(repo_root, "rev-parse", "HEAD")
        if head_after and head_after != turn.baseline_head:
            out = _git_output(repo_root, "diff", "--name-only", turn.baseline_head, head_after)
            if out:
                files.update(line for line in out.splitlines() if line)

    files.update(_changed_sensitive_files(repo_root, turn.baseline_sensitive_files))
    return sorted(files)


def _status_entries(status_z: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for entry in status_z.split("\0"):
        if not entry:
            continue
        code = entry[:2].strip() or "??"
        path = entry[3:] if len(entry) > 3 else entry
        if path:
            entries[path.split(" -> ")[-1]] = code
    return entries


def _fingerprints_for(repo_root: Path | None, entries: dict[str, str]) -> dict[str, str]:
    if repo_root is None:
        return {}
    out: dict[str, str] = {}
    for rel in entries:
        path = (repo_root / rel).resolve()
        if not _safe_child(repo_root, path) or not path.is_file():
            out[rel] = "missing"
            continue
        out[rel] = _file_fingerprint(path)
    return out


def _sensitive_file_fingerprints(repo_root: Path | None) -> dict[str, str]:
    if repo_root is None:
        return {}
    out: dict[str, str] = {}
    for path in _sensitive_files(repo_root):
        rel = path.relative_to(repo_root).as_posix()
        out[rel] = _file_fingerprint(path)
    return out


def _changed_sensitive_files(repo_root: Path | None, baseline: dict[str, str]) -> list[str]:
    if repo_root is None:
        return []
    current = _sensitive_file_fingerprints(repo_root)
    changed = [
        rel
        for rel, fingerprint in current.items()
        if baseline.get(rel) != fingerprint
    ]
    removed = [rel for rel in baseline if rel not in current]
    return sorted(set(changed + removed))


def _sensitive_files(repo_root: Path) -> list[Path]:
    paths: list[Path] = []
    for pattern in (".env", ".env.*", "*.pem", "*.key"):
        for path in repo_root.glob(pattern):
            if path.is_file() and _safe_child(repo_root, path.resolve()):
                paths.append(path.resolve())
    return sorted(set(paths))


def _file_fingerprint(path: Path) -> str:
    try:
        stat = path.stat()
        h = hashlib.sha256()
        h.update(str(stat.st_size).encode())
        with path.open("rb") as f:
            h.update(f.read(min(stat.st_size, _MAX_FILE_BYTES)))
        return h.hexdigest()
    except Exception:
        return "unreadable"


def _checks_config_path(repo_root: Path) -> Path | None:
    for rel in (".babata/checks.json", ".babata/checks.jsonc"):
        path = repo_root / rel
        if path.is_file():
            return path
    return None


def _check_context(changed_files: list[str], guard_findings: list[dict[str, Any]]) -> set[str]:
    ctx = {"always"}
    for rel in changed_files:
        suffix = Path(rel).suffix.lower()
        if suffix == ".py":
            ctx.add("python")
        if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs"}:
            ctx.add("javascript")
            ctx.add("node")
        if suffix in {".md", ".txt"} or Path(rel).name in {"AGENTS.md", "CLAUDE.md"}:
            ctx.add("docs")
            ctx.add("prompt")
        if _is_ops_path(rel):
            ctx.add("ops")
        if ".claude" in rel or "hook" in rel.lower():
            ctx.add("hooks")
    if guard_findings:
        ctx.add("security")
    return ctx


def _check_matches(when: Any, context: set[str]) -> bool:
    if isinstance(when, str):
        return when in context
    if isinstance(when, list):
        expected = {str(item) for item in when}
        return bool(expected & context)
    return False


def _check_timeout(raw: Any) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = _DEFAULT_CHECK_TIMEOUT_SECONDS
    return min(max(value, 1.0), 1800.0)


def _review_kinds(
    changed_files: list[str],
    guard_findings: list[dict[str, Any]],
    check_results: list[dict[str, Any]],
) -> list[str]:
    kinds: list[str] = []
    if guard_findings or any(_security_path(path) for path in changed_files):
        kinds.append("security")
    if any(_is_ops_path(path) for path in changed_files):
        kinds.append("ops")
    if any(_prompt_or_hook_path(path) for path in changed_files):
        kinds.append("prompt_policy")
    failed_checks = any(item.get("status") in {"failed", "timeout", "error", "config_error"} for item in check_results)
    if changed_files or failed_checks:
        kinds.append("general_code")
    return _ordered_unique(kinds)


def _reviewer_for(cpu: str, kind: str) -> str:
    if kind == "security":
        return "claude-security-guidance"
    if cpu == "codex":
        return "claude"
    if cpu == "claude":
        return "codex"
    return "independent"


def _public_summary(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "turn_id": record.get("turn_id"),
        "ledger_path": str(_ledger_path()),
        "review_bus_path": str(_review_bus_path()),
        "repo_root": record.get("repo_root"),
        "baseline_head": ((record.get("git") or {}).get("baseline_head")),
        "head_after": ((record.get("git") or {}).get("head_after")),
        "changed_files": ((record.get("git") or {}).get("changed_files") or []),
        "guard_findings": record.get("guard_findings") or [],
        "declared_checks": record.get("declared_checks") or [],
        "review_tasks": record.get("review_tasks") or [],
    }


def _compact_tool_uses(tool_uses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for tool in tool_uses:
        item = dict(tool)
        if isinstance(item.get("command"), str):
            item["command"] = _preview(item["command"], _MAX_COMMAND_PREVIEW)
        compact.append(item)
    return compact[:100]


def _tool_command(tool: dict[str, Any]) -> str | None:
    for key in ("command", "cmd"):
        value = tool.get(key)
        if isinstance(value, str):
            return value
    raw = tool.get("input")
    if isinstance(raw, dict):
        for key in ("command", "cmd"):
            value = raw.get(key)
            if isinstance(value, str):
                return value
    return None


def _tool_path(tool: dict[str, Any]) -> str | None:
    for key in ("file_path", "path", "notebook_path"):
        value = tool.get(key)
        if isinstance(value, str):
            return value.replace("\\", "/")
    raw = tool.get("input")
    if isinstance(raw, dict):
        for key in ("file_path", "path", "notebook_path"):
            value = raw.get(key)
            if isinstance(value, str):
                return value.replace("\\", "/")
    return None


def _tool_content_text(value: Any) -> str:
    if isinstance(value, dict):
        parts: list[str] = []
        for key in ("content", "new_string", "old_string", "text"):
            raw = value.get(key)
            if isinstance(raw, str):
                parts.append(raw)
        edits = value.get("edits")
        if isinstance(edits, list):
            for edit in edits:
                parts.append(_tool_content_text(edit))
        return "\n".join(part for part in parts if part)
    return ""


def _finding(
    severity: str,
    rule: str,
    message: str,
    *,
    path: str | None = None,
    command: str | None = None,
    blocking: bool = False,
) -> dict[str, Any]:
    out: dict[str, Any] = {
        "severity": severity,
        "rule": rule,
        "message": message,
        "blocking": blocking,
    }
    if path:
        out["path"] = path
    if command:
        out["command"] = _preview(command, _MAX_COMMAND_PREVIEW)
    return out


def _dedupe_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for finding in findings:
        key = (finding.get("rule"), finding.get("path"), finding.get("command"))
        if key in seen:
            continue
        seen.add(key)
        out.append(finding)
    return out


def _is_ops_path(path: str) -> bool:
    lower = path.lower()
    return (
        lower.endswith(".plist")
        or "launchagent" in lower
        or lower in {"scripts/self-ops.sh", "scripts/auto-update.sh", "scripts/poll-healthcheck.sh"}
        or "self-ops" in lower
        or "auto-update" in lower
    )


def _security_path(path: str) -> bool:
    lower = path.lower()
    name = Path(lower).name
    return (
        name.startswith(".env")
        or name in {"security.md", "claude-security-guidance.md"}
        or "security" in lower
        or "secret" in lower
        or "token" in lower
    )


def _prompt_or_hook_path(path: str) -> bool:
    lower = path.lower()
    name = Path(lower).name
    return (
        name in {"agents.md", "claude.md", "codex.md"}
        or lower.startswith(".claude/")
        or "/hooks/" in lower
        or lower.startswith("hooks/")
        or "skill" in lower and lower.endswith(".md")
    )


def _looks_text(path: Path) -> bool:
    if path.suffix.lower() in _TEXT_SUFFIXES:
        return True
    try:
        with path.open("rb") as f:
            sample = f.read(2048)
        return b"\0" not in sample
    except Exception:
        return False


def _read_small_text(path: Path) -> str:
    try:
        if path.stat().st_size > _MAX_FILE_BYTES:
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def _safe_child(root: Path, child: Path) -> bool:
    try:
        child.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _ordered_unique(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _text_bytes(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _error_record(error: BaseException | None) -> dict[str, Any] | None:
    if error is None:
        return None
    message = str(error)
    return {
        "type": type(error).__name__,
        "message_preview": _preview(message, _MAX_ERROR_PREVIEW),
        "message_sha256": _sha256_text(message),
        "message_bytes": _text_bytes(message),
    }


def _preview(text: str, limit: int) -> str:
    text = text.replace("\0", "")
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _tail(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return "..." + text[-limit:]


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))
