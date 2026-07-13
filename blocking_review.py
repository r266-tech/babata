"""Synchronous review gate for code-changing babata turns.

This is a Stop-style gate: review happens before the engine returns/yields final
completion. It intentionally does not enqueue async advisory work into the live
conversation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from cli_runtime import env_cli_path
from constants import NAMESPACE, STATE_DIR


_CODE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".mjs",
    ".plist",
    ".py",
    ".rs",
    ".sh",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
_MAX_FINDING_TEXT = 1600
_MAX_REPAIR_PROMPT = 6000
_MAX_REVIEW_CONTEXT = 24000
_MAX_REVIEW_PROMPT = 32000
_MAX_RAW_OUTPUT = 4000
_MIN_RESPONSE_SCRUB_CHARS = 32
_REVIEW_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "status": {"type": "string", "enum": ["passed", "needs_fix"]},
        "message": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": True,
                "properties": {
                    "severity": {"type": "string"},
                    "rule": {"type": "string"},
                    "path": {"type": "string"},
                    "message": {"type": "string"},
                },
                "required": ["severity", "rule", "message"],
            },
        },
    },
    "required": ["status", "findings"],
}


def blocking_review_max_rounds() -> int:
    raw = os.environ.get("BABATA_BLOCKING_REVIEW_MAX_ROUNDS", "2")
    try:
        value = int(raw)
    except ValueError:
        return 2
    return min(max(value, 0), 5)


def run_blocking_review(
    audit_summary: dict[str, Any] | None,
    *,
    cpu: str,
    channel: str,
    response_content: str,
    round_index: int,
) -> dict[str, Any]:
    if os.environ.get("BABATA_BLOCKING_REVIEW", "1") == "0":
        return _result("skipped", reason="BABATA_BLOCKING_REVIEW=0", round_index=round_index)
    if not _needs_review(audit_summary):
        return _result("skipped", reason="no code-changing turn", round_index=round_index)

    response_preview = _trim(response_content, 2000)
    payload = {
        "schema": "babata.blocking_review_payload.v1",
        "cpu": cpu,
        "channel": channel,
        "round_index": round_index,
        "audit": audit_summary or {},
        "response_preview": response_preview,
    }
    command = os.environ.get("BABATA_BLOCKING_REVIEW_CMD")
    if command:
        result = _run_review_command(command, payload, round_index)
    else:
        result = _review_without_command(payload, round_index)
    result = _scrub_response_from_review_result(
        result,
        response_content=response_content,
        response_preview=response_preview,
    )
    _record_review_result(result)
    return result


def build_repair_prompt(review: dict[str, Any]) -> str:
    findings = _format_findings(review.get("findings") or [])
    if not findings:
        findings = f"- {review.get('message') or review.get('reason') or 'Blocking review requested another pass.'}"
    prompt = f"""<blocking-review>
Previous code-changing turn failed blocking review.

Fix findings in the same repository/session. Do not ask implementation details.
Rerun relevant checks, then answer only with the final result.

Findings:
{findings}
</blocking-review>"""
    return _trim(prompt, _MAX_REPAIR_PROMPT)


def unresolved_review_message(response_content: str, review: dict[str, Any]) -> str:
    findings = _format_findings(review.get("findings") or [])
    if not findings:
        findings = f"- {review.get('message') or review.get('reason') or 'Blocking review did not pass.'}"
    return (
        "Blocking review did not pass after the configured repair rounds.\n\n"
        f"Findings:\n{findings}\n\n"
        f"Last response draft:\n{response_content}"
    )


def _needs_review(audit_summary: dict[str, Any] | None) -> bool:
    if not isinstance(audit_summary, dict):
        return False
    findings = audit_summary.get("guard_findings") or []
    if findings:
        return True
    checks = audit_summary.get("declared_checks") or []
    if any(_check_failed(item) for item in checks if isinstance(item, dict)):
        return True
    changed_files = audit_summary.get("changed_files") or _changed_files_from_tasks(audit_summary)
    return any(_is_code_path(str(path)) for path in changed_files)


def _changed_files_from_tasks(audit_summary: dict[str, Any]) -> list[str]:
    files: list[str] = []
    for task in audit_summary.get("review_tasks") or []:
        for path in task.get("changed_files") or []:
            if path not in files:
                files.append(path)
    return files


def _is_code_path(path: str) -> bool:
    name = Path(path).name
    if name in {"pyproject.toml", "package.json", "tsconfig.json", "uv.lock"}:
        return True
    return Path(path).suffix.lower() in _CODE_SUFFIXES


def _check_failed(item: dict[str, Any]) -> bool:
    return item.get("status") in {"failed", "timeout", "error", "config_error"}


def _deterministic_review(payload: dict[str, Any], round_index: int) -> dict[str, Any]:
    audit = payload.get("audit") or {}
    findings: list[dict[str, Any]] = []
    for finding in audit.get("guard_findings") or []:
        if finding.get("blocking") or finding.get("severity") == "high":
            findings.append({
                "severity": finding.get("severity") or "high",
                "rule": finding.get("rule") or "guard",
                "message": finding.get("message") or "Deterministic guard finding.",
                "path": finding.get("path"),
            })
    for check in audit.get("declared_checks") or []:
        if isinstance(check, dict) and _check_failed(check):
            findings.append({
                "severity": "high",
                "rule": f"declared-check:{check.get('name') or 'unnamed'}",
                "message": f"Declared check status={check.get('status')} exit={check.get('exit_code')}",
            })
    if findings:
        return _result(
            "needs_fix",
            findings=findings,
            reviewer="deterministic",
            round_index=round_index,
        )
    return _result("passed", reviewer="deterministic", round_index=round_index)


def _review_without_command(payload: dict[str, Any], round_index: int) -> dict[str, Any]:
    result = _deterministic_review(payload, round_index)
    if result.get("status") == "needs_fix":
        return result
    if not _counterpart_review_enabled():
        return result
    if _current_review_depth() >= _max_review_depth():
        result["reason"] = "counterpart review skipped inside delegated reviewer"
        return result
    return _run_counterpart_review(payload, round_index)


def _counterpart_review_enabled() -> bool:
    mode = os.environ.get("BABATA_BLOCKING_REVIEW_AGENT", "counterpart").strip().lower()
    if mode in {"0", "off", "false", "disabled", "none", "deterministic"}:
        return False
    return os.environ.get("BABATA_BLOCKING_REVIEW_COUNTERPART", "1") != "0"


def _current_review_depth() -> int:
    raw = os.environ.get("BABATA_BLOCKING_REVIEW_DEPTH", "0")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _max_review_depth() -> int:
    raw = os.environ.get("BABATA_BLOCKING_REVIEW_MAX_DEPTH", "1")
    try:
        return max(0, int(raw))
    except ValueError:
        return 1


def _run_counterpart_review(payload: dict[str, Any], round_index: int) -> dict[str, Any]:
    source_cpu = str(payload.get("cpu") or "").lower()
    review_cpu = os.environ.get("BABATA_BLOCKING_REVIEW_CPU", "codex").strip().lower()
    if review_cpu in {"claude", "cc", "claude-code"}:
        return _run_claude_counterpart_review(payload, round_index)
    if review_cpu in {"codex", ""}:
        return _run_codex_counterpart_review(payload, round_index)
    # Explicit compatibility mode for the historical opposite-CPU policy.
    # It is never the default while Claude is unavailable.
    if review_cpu in {"counterpart", "opposite"}:
        if source_cpu == "codex":
            return _run_claude_counterpart_review(payload, round_index)
        if source_cpu == "claude":
            return _run_codex_counterpart_review(payload, round_index)
    # A typo or stale value must not silently disable the model review gate.
    # Codex is the safe current default while Claude is unavailable.
    return _run_codex_counterpart_review(payload, round_index)


def _run_claude_counterpart_review(payload: dict[str, Any], round_index: int) -> dict[str, Any]:
    cli = _cc_worker_cli()
    if cli is None:
        return _review_infra_failure("claude-counterpart", "cc-worker CLI not found", round_index)
    repo_root = _repo_root(payload)
    if repo_root is None:
        return _result("passed", reviewer="deterministic", reason="no repo root for counterpart review", round_index=round_index)

    timeout = _review_timeout_seconds()
    worker_name = _worker_name(payload, round_index)
    prompt = _build_counterpart_review_prompt(payload, reviewer="claude")
    args = [
        str(cli),
        "start",
        worker_name,
        "--cwd",
        str(repo_root),
        "--role",
        "review",
        "--permission-mode",
        os.environ.get("BABATA_CC_REVIEW_PERMISSION_MODE", "dontAsk"),
        "--max-turns",
        os.environ.get("BABATA_CC_REVIEW_MAX_TURNS", "8"),
        "--origin-cpu",
        "codex",
        "--delegation-depth",
        "1",
        "--max-delegation-depth",
        "1",
        "--parent-task-id",
        _metadata_token(_turn_id(payload)),
        "--replace",
        "--timeout",
        str(int(timeout)),
        "--prompt",
        prompt,
    ]
    started = time.time()
    try:
        proc = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout + 10,
            env=_review_child_env(payload),
        )
    except subprocess.TimeoutExpired as e:
        output = "\n".join(str(part) for part in (e.stdout, e.stderr) if part)
        return _review_timeout("claude-counterpart", timeout, output, round_index)
    finally:
        _remove_cc_worker(cli, worker_name)

    raw = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    text, nested_exit_code, nested_timed_out = _cc_worker_text(proc.stdout)
    result = _parse_command_result(text or raw)
    result["reviewer"] = "claude-counterpart"
    result["round_index"] = round_index
    result["duration_ms"] = round((time.time() - started) * 1000)
    result["exit_code"] = proc.returncode
    if proc.returncode != 0 or nested_exit_code not in (None, 0) or nested_timed_out:
        if _looks_like_counterpart_infra_failure(raw or text):
            message = _counterpart_infra_failure_message(
                raw or text,
                f"cc-worker exited {proc.returncode}",
            )
            result = _review_infra_failure(
                "claude-counterpart",
                message,
                round_index,
            )
            result["duration_ms"] = round((time.time() - started) * 1000)
            result["exit_code"] = proc.returncode
            return result
        _force_review_failure(
            result,
            "counterpart-review-failed",
            _trim(raw or text or f"cc-worker exited {proc.returncode}", _MAX_FINDING_TEXT),
        )
    return result


def _run_codex_counterpart_review(payload: dict[str, Any], round_index: int) -> dict[str, Any]:
    cli = _codex_cli()
    if cli is None:
        return _review_infra_failure("codex-counterpart", "codex CLI not found", round_index)
    repo_root = _repo_root(payload)
    if repo_root is None:
        return _result("passed", reviewer="deterministic", reason="no repo root for counterpart review", round_index=round_index)

    timeout = _review_timeout_seconds()
    prompt = _build_counterpart_review_prompt(payload, reviewer="codex")
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="babata-review-") as td:
        tmp = Path(td)
        schema_path = tmp / "review.schema.json"
        output_path = tmp / "review-output.json"
        schema_path.write_text(json.dumps(_REVIEW_SCHEMA), encoding="utf-8")
        args = [
            str(cli),
            "exec",
            "-C",
            str(repo_root),
            "--sandbox",
            os.environ.get("BABATA_CODEX_REVIEW_SANDBOX", "read-only"),
            "--ask-for-approval",
            "never",
            "--skip-git-repo-check",
            "--ephemeral",
            "--output-schema",
            str(schema_path),
            "--output-last-message",
            str(output_path),
        ]
        model = os.environ.get("BABATA_CODEX_REVIEW_MODEL") or "gpt-5.6-sol"
        reasoning = os.environ.get("BABATA_CODEX_REVIEW_REASONING") or "max"
        args.extend([
            "--model",
            model,
            "-c",
            f'model_reasoning_effort="{reasoning}"',
        ])
        args.append("-")
        try:
            proc = subprocess.run(
                args,
                input=prompt,
                text=True,
                capture_output=True,
                timeout=timeout,
                env=_review_child_env(payload),
            )
        except subprocess.TimeoutExpired as e:
            output = "\n".join(str(part) for part in (e.stdout, e.stderr) if part)
            return _review_timeout("codex-counterpart", timeout, output, round_index)

        output_text = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
    raw = "\n".join(part for part in (output_text, proc.stdout, proc.stderr) if part).strip()
    result = _parse_command_result(output_text or raw)
    result["reviewer"] = "codex-counterpart"
    result["round_index"] = round_index
    result["duration_ms"] = round((time.time() - started) * 1000)
    result["exit_code"] = proc.returncode
    if proc.returncode != 0:
        if _looks_like_counterpart_infra_failure(raw):
            message = _counterpart_infra_failure_message(
                raw,
                f"codex exited {proc.returncode}",
            )
            result = _review_infra_failure(
                "codex-counterpart",
                message,
                round_index,
            )
            result["duration_ms"] = round((time.time() - started) * 1000)
            result["exit_code"] = proc.returncode
            return result
        _force_review_failure(
            result,
            "counterpart-review-failed",
            _trim(raw or f"codex exited {proc.returncode}", _MAX_FINDING_TEXT),
        )
    return result


def _run_review_command(command: str, payload: dict[str, Any], round_index: int) -> dict[str, Any]:
    timeout = _review_timeout_seconds()
    payload_text = json.dumps(payload, ensure_ascii=False)
    started = time.time()
    env = {
        **_review_child_env(payload),
        "BABATA_REVIEW_PAYLOAD": payload_text,
    }
    try:
        proc = subprocess.run(
            command,
            input=payload_text,
            text=True,
            shell=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        output = "\n".join(str(part) for part in (e.stdout, e.stderr) if part)
        return _result(
            "needs_fix",
            reviewer="command",
            message=f"blocking review command timed out after {timeout}s",
            findings=[{"severity": "high", "rule": "review-timeout", "message": _trim(output, _MAX_FINDING_TEXT)}],
            round_index=round_index,
        )
    raw = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
    parsed = _parse_command_result(raw)
    parsed["reviewer"] = parsed.get("reviewer") or "command"
    parsed["round_index"] = round_index
    parsed["duration_ms"] = round((time.time() - started) * 1000)
    parsed["exit_code"] = proc.returncode
    if proc.returncode != 0 and parsed.get("status") == "passed":
        parsed["status"] = "needs_fix"
    if proc.returncode != 0 and not parsed.get("findings"):
        parsed["findings"] = [{
            "severity": "high",
            "rule": "review-command-failed",
            "message": _trim(raw or f"review command exited {proc.returncode}", _MAX_FINDING_TEXT),
        }]
    return parsed


def _parse_command_result(raw: str) -> dict[str, Any]:
    data = _extract_json_object(raw)
    if isinstance(data, dict):
        return _normalize_review_result(data, raw)
    upper = raw.upper()
    status = "needs_fix" if any(marker in upper for marker in ("NEEDS_FIX", "NEEDS-FIX", "BLOCKER", "FAIL")) else "passed"
    findings = []
    if status == "needs_fix":
        findings = [{"severity": "high", "rule": "review", "message": _trim(raw, _MAX_FINDING_TEXT)}]
    return {"status": status, "findings": findings, "raw_output": _trim(raw, _MAX_FINDING_TEXT)}


def _extract_json_object(raw: str) -> dict[str, Any] | None:
    text = raw.strip()
    if not text:
        return None
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        return data
    decoder = json.JSONDecoder()
    for match in re.finditer(r"\{", text):
        try:
            data, _ = decoder.raw_decode(text[match.start():])
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            return data
    return None


def _normalize_review_result(data: dict[str, Any], raw: str) -> dict[str, Any]:
    status = str(data.get("status") or data.get("verdict") or "").lower()
    if status in {"pass", "passed", "ok", "approve", "approved"}:
        status = "passed"
    elif status in {"needs_fix", "needs-fix", "fail", "failed", "block", "blocked"}:
        status = "needs_fix"
    else:
        status = "needs_fix" if data.get("findings") else "passed"
    findings = data.get("findings") or []
    if isinstance(findings, str):
        findings = [{"severity": "medium", "rule": "review", "message": findings}]
    return {
        "status": status,
        "findings": findings if isinstance(findings, list) else [],
        "message": data.get("message"),
    }


def _result(
    status: str,
    *,
    reason: str | None = None,
    message: str | None = None,
    findings: list[dict[str, Any]] | None = None,
    reviewer: str = "none",
    round_index: int = 0,
) -> dict[str, Any]:
    return {
        "schema": "babata.blocking_review_result.v1",
        "status": status,
        "reason": reason,
        "message": message,
        "findings": findings or [],
        "reviewer": reviewer,
        "round_index": round_index,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _scrub_response_from_review_result(
    result: dict[str, Any],
    *,
    response_content: str,
    response_preview: str,
) -> dict[str, Any]:
    marker = _response_marker(response_content)
    needles = _response_scrub_needles(response_content, response_preview)
    scrubbed = _scrub_value(result, needles, marker)
    if not isinstance(scrubbed, dict):
        scrubbed = dict(result)
    scrubbed["response_sha256"] = _sha256_text(response_content)
    scrubbed["response_bytes"] = _text_bytes(response_content)
    return scrubbed


def _response_scrub_needles(response_content: str, response_preview: str) -> list[str]:
    needles: list[str] = []
    for value in (response_content, response_preview):
        if len(value) < _MIN_RESPONSE_SCRUB_CHARS:
            continue
        if value not in needles:
            needles.append(value)
    return needles


def _scrub_value(value: Any, needles: list[str], marker: str) -> Any:
    if isinstance(value, str):
        for needle in needles:
            value = value.replace(needle, marker)
        return value
    if isinstance(value, list):
        return [_scrub_value(item, needles, marker) for item in value]
    if isinstance(value, dict):
        return {
            key: _scrub_value(item, needles, marker)
            for key, item in value.items()
        }
    return value


def _response_marker(response_content: str) -> str:
    return f"[response draft omitted sha256={_sha256_text(response_content)}]"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _text_bytes(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _build_counterpart_review_prompt(payload: dict[str, Any], *, reviewer: str) -> str:
    source_cpu = str(payload.get("cpu") or "unknown")
    audit = payload.get("audit") if isinstance(payload.get("audit"), dict) else {}
    review_context = _review_context(audit)
    instructions = f"""You are the synchronous blocking review gate.

Review source_cpu={source_cpu}; reviewer={reviewer}. Read-only sidecar: do not
edit files, call another CPU, or delegate. Source CPU applies fixes.

Return JSON only:
{{"status":"passed","findings":[]}}
or
{{"status":"needs_fix","findings":[{{"severity":"high","rule":"bug","path":"path","message":"specific issue"}}]}}

Block only concrete bugs, regressions, security/privacy issues, broken checks,
or clear request violations. Do not block style, speculation, nice-to-have tests,
or preferences.

Audit summary:
{json.dumps(_compact_audit_for_prompt(audit), ensure_ascii=False, separators=(",", ":"))}

Response draft:
{payload.get("response_preview") or ""}

Review context:
{review_context}
"""
    return _trim(instructions, _MAX_REVIEW_PROMPT)


def _compact_audit_for_prompt(audit: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "turn_id",
        "repo_root",
        "baseline_head",
        "head_after",
        "changed_files",
        "guard_findings",
        "declared_checks",
    }
    return {key: audit.get(key) for key in keep if key in audit}


def _review_context(audit: dict[str, Any]) -> str:
    repo_root = _repo_root({"audit": audit})
    if repo_root is None:
        return "No git repo root was available."
    changed_files = [str(path) for path in (audit.get("changed_files") or [])]
    if not changed_files:
        return "No changed files were attributed to this turn."

    base = audit.get("baseline_head")
    head_after = audit.get("head_after")
    parts: list[str] = []
    if base and head_after and base != head_after:
        diff = _git_capture(repo_root, ["diff", str(base), str(head_after), "--", *changed_files])
        working_diff = _git_capture(repo_root, ["diff", str(head_after), "--", *changed_files])
        if working_diff:
            diff = f"{diff}\n\n## Working tree after commit\n{working_diff}" if diff else working_diff
    elif base:
        diff = _git_capture(repo_root, ["diff", str(base), "--", *changed_files])
    else:
        diff = _git_capture(repo_root, ["diff", "--", *changed_files])
    if diff:
        parts.append("## Working tree diff\n" + diff)
    else:
        parts.append("## Working tree diff\n(no textual git diff)")

    for rel in changed_files:
        path = (repo_root / rel).resolve()
        if not _safe_child(repo_root, path) or not path.is_file() or not _is_code_path(rel):
            continue
        text = _read_small_text(path, limit=8000)
        if text:
            parts.append(f"## Current file: {rel}\n{text}")
        if len("\n\n".join(parts)) >= _MAX_REVIEW_CONTEXT:
            break
    return _trim("\n\n".join(parts), _MAX_REVIEW_CONTEXT)


def _git_capture(repo_root: Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), *args],
            text=True,
            capture_output=True,
            timeout=20,
        )
    except Exception:
        return ""
    if proc.returncode != 0:
        return _trim(proc.stderr, _MAX_REVIEW_CONTEXT)
    return _trim(proc.stdout, _MAX_REVIEW_CONTEXT)


def _repo_root(payload: dict[str, Any]) -> Path | None:
    audit = payload.get("audit") if isinstance(payload.get("audit"), dict) else {}
    root = audit.get("repo_root")
    if not root:
        return None
    try:
        path = Path(str(root)).expanduser().resolve()
    except Exception:
        return None
    return path if path.is_dir() else None


def _read_small_text(path: Path, *, limit: int) -> str:
    try:
        data = path.read_bytes()[:limit]
    except Exception:
        return ""
    if b"\0" in data:
        return ""
    return data.decode("utf-8", errors="replace")


def _safe_child(root: Path, child: Path) -> bool:
    try:
        child.relative_to(root)
    except ValueError:
        return False
    return True


def _review_child_env(payload: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    env["BABATA_BLOCKING_REVIEW"] = "0"
    env["BABATA_BLOCKING_REVIEW_DEPTH"] = str(_current_review_depth() + 1)
    env.setdefault("BABATA_REVIEW_SOURCE_CPU", str(payload.get("cpu") or ""))
    env.setdefault("BABATA_REVIEW_MODE", "counterpart")
    return env


def _cc_worker_cli() -> Path | None:
    configured = os.environ.get("BABATA_CC_WORKER")
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() and os.access(path, os.X_OK) else None
    candidates = [
        Path.home() / "cc-workspace/bin/cc-worker",
    ]
    for candidate in candidates:
        if candidate and candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("cc-worker")
    return Path(found) if found else None


def _codex_cli() -> Path | None:
    configured = env_cli_path(
        "BABATA_CODEX_REVIEW_CLI",
        "BABATA_CODEX_CLI_PATH",
        "CODEX_CLI_PATH",
    )
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() and os.access(path, os.X_OK) else None
    found = shutil.which("codex")
    return Path(found) if found else None


def _cc_worker_text(stdout: str) -> tuple[str, int | None, bool]:
    try:
        data = json.loads(stdout)
    except Exception:
        return stdout, None, False
    turn = data.get("initial_turn") or data.get("turn") or {}
    if not isinstance(turn, dict):
        return stdout, None, False
    text = str(turn.get("text") or "")
    exit_code = turn.get("exit_code")
    timed_out = bool(turn.get("timed_out"))
    return text, exit_code if isinstance(exit_code, int) else None, timed_out


def _remove_cc_worker(cli: Path, name: str) -> None:
    try:
        subprocess.run(
            [str(cli), "remove", name],
            text=True,
            capture_output=True,
            timeout=15,
            env=_review_child_env({"cpu": "cleanup"}),
        )
    except Exception:
        return


def _worker_name(payload: dict[str, Any], round_index: int) -> str:
    turn_id = _metadata_token(_turn_id(payload))
    return f"babata-review-{turn_id}-{round_index}-{uuid.uuid4().hex[:6]}"[:80]


def _turn_id(payload: dict[str, Any]) -> str:
    audit = payload.get("audit") if isinstance(payload.get("audit"), dict) else {}
    return str(audit.get("turn_id") or f"turn-{int(time.time())}")


def _metadata_token(value: str) -> str:
    token = re.sub(r"[^A-Za-z0-9._:/-]", "-", value)
    return token[:120] or "unknown"


def _review_timeout(reviewer: str, timeout: float, output: str, round_index: int) -> dict[str, Any]:
    return _result(
        "needs_fix",
        reviewer=reviewer,
        message=f"blocking review timed out after {timeout:g}s",
        findings=[{"severity": "high", "rule": "review-timeout", "message": _trim(output, _MAX_FINDING_TEXT)}],
        round_index=round_index,
    )


def _review_infra_failure(reviewer: str, message: str, round_index: int) -> dict[str, Any]:
    if os.environ.get("BABATA_BLOCKING_REVIEW_INFRA_STRICT", "0") == "1":
        return _result(
            "needs_fix",
            reviewer=reviewer,
            message=message,
            findings=[{"severity": "high", "rule": "review-infra", "message": message}],
            round_index=round_index,
        )
    return _result("passed", reviewer=reviewer, reason=message, round_index=round_index)


def _looks_like_counterpart_infra_failure(output: str) -> bool:
    text = output.lower()
    markers = (
        "invalid api key",
        "incorrect api key",
        "api_error_status\": 401",
        "api_error_status': 401",
        "authentication failed",
        "unauthorized",
        "fix external api key",
    )
    return any(marker in text for marker in markers)


def _counterpart_infra_failure_message(output: str, fallback: str) -> str:
    data = _extract_json_object(output)
    candidates: list[Any] = []
    if isinstance(data, dict):
        worker = data.get("worker")
        if isinstance(worker, dict):
            candidates.append(worker.get("last_error"))
        turn = data.get("initial_turn") or data.get("turn")
        if isinstance(turn, dict):
            candidates.append(turn.get("text"))
            parsed = turn.get("parsed")
            if isinstance(parsed, dict):
                candidates.append(parsed.get("result"))
    candidates.append(output)
    for candidate in candidates:
        if isinstance(candidate, str) and _looks_like_counterpart_infra_failure(candidate):
            return _trim(f"counterpart reviewer infrastructure unavailable: {candidate}", _MAX_FINDING_TEXT)
    return _trim(f"counterpart reviewer infrastructure unavailable: {fallback}", _MAX_FINDING_TEXT)


def _force_review_failure(result: dict[str, Any], rule: str, message: str) -> None:
    result["status"] = "needs_fix"
    findings = result.get("findings")
    if not isinstance(findings, list):
        findings = []
    if not findings:
        findings.append({"severity": "high", "rule": rule, "message": message})
    result["findings"] = findings


def _format_findings(findings: list[Any]) -> str:
    lines: list[str] = []
    for raw in findings[:20]:
        if isinstance(raw, dict):
            severity = raw.get("severity") or "medium"
            rule = raw.get("rule") or "review"
            path = f" {raw.get('path')}" if raw.get("path") else ""
            message = raw.get("message") or raw.get("body") or raw.get("text") or raw
            lines.append(f"- [{severity}] {rule}{path}: {message}")
        else:
            lines.append(f"- {raw}")
    return _trim("\n".join(lines), _MAX_REPAIR_PROMPT)


def _record_review_result(result: dict[str, Any]) -> None:
    try:
        path = _audit_dir() / f"{NAMESPACE}-blocking-review.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        return


def _audit_dir() -> Path:
    path = Path(os.environ.get("BABATA_AUDIT_DIR", str(STATE_DIR / "audit"))).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


def _review_timeout_seconds() -> float:
    raw = os.environ.get("BABATA_BLOCKING_REVIEW_TIMEOUT", "240")
    try:
        value = float(raw)
    except ValueError:
        return 240.0
    return min(max(value, 1.0), 1800.0)


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."
