import asyncio
import json
import subprocess
import sys
from pathlib import Path

from claude_agent_sdk.types import ToolPermissionContext

import cc
import turn_audit
from cc import Response


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=path, check=True, capture_output=True)


def _jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_turn_audit_records_ledger_guards_checks_and_review_bus(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    audit_dir = tmp_path / "audit"
    monkeypatch.setenv("BABATA_TURN_LEDGER", "1")
    monkeypatch.setenv("BABATA_DETERMINISTIC_GUARDS", "observe")
    monkeypatch.setenv("BABATA_DECLARED_CHECKS", "1")
    monkeypatch.setenv("BABATA_REVIEW_BUS", "queue")
    monkeypatch.setenv("BABATA_AUDIT_DIR", str(audit_dir))

    checks_dir = repo / ".babata"
    checks_dir.mkdir()
    (checks_dir / "checks.json").write_text(json.dumps({
        "checks": [
            {
                "name": "smoke",
                "command": f"{sys.executable} -c 'print(\"ok\")'",
                "when": ["always"],
            }
        ]
    }))
    subprocess.run(["git", "add", ".babata/checks.json"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "checks"], cwd=repo, check=True, capture_output=True)

    turn = turn_audit.begin_turn(
        cpu="codex",
        channel="test",
        prompt="please edit",
        session_id_before="sid-0",
        cwd=repo,
    )
    assert turn is not None

    (repo / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-testsecret0000000000000000\n")

    summary = turn_audit.finish_turn(
        turn,
        response=Response(content="done", session_id="sid-1", cost=0.0, tools=["Bash"]),
        tools=["Bash"],
        tool_uses=[{"name": "Bash", "command": "git status --short"}],
    )

    assert summary is not None
    assert any(f["rule"] == "env-file-changed" for f in summary["guard_findings"])
    assert any(f["rule"].startswith("secret-pattern") for f in summary["guard_findings"])
    assert summary["declared_checks"][0]["name"] == "smoke"
    assert summary["declared_checks"][0]["status"] == "passed"
    assert {task["kind"] for task in summary["review_tasks"]} >= {"security", "general_code"}

    ledger = _jsonl(audit_dir / "babata-turn-ledger.jsonl")
    assert [row["event"] for row in ledger] == ["begin", "finish"]
    assert ".env" in ledger[-1]["git"]["changed_files"]
    review_rows = _jsonl(audit_dir / "babata-review-bus.jsonl")
    assert review_rows
    assert review_rows[0]["source_cpu"] == "codex"


def test_turn_audit_caps_prompt_and_final_previews(monkeypatch, tmp_path):
    audit_dir = tmp_path / "audit"
    monkeypatch.setenv("BABATA_TURN_LEDGER", "1")
    monkeypatch.setenv("BABATA_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("BABATA_REVIEW_BUS", "off")
    long_prompt = "prompt-" + ("p" * 400) + "-PROMPT-TAIL"
    long_final = "answer-" + ("a" * 500) + "-FINAL-TAIL"

    turn = turn_audit.begin_turn(
        cpu="codex",
        channel="test",
        prompt=long_prompt,
        session_id_before=None,
        cwd=tmp_path,
    )
    assert turn is not None
    summary = turn_audit.finish_turn(
        turn,
        response=Response(content=long_final, session_id="sid-1", cost=0.0),
    )

    assert summary is not None
    raw = (audit_dir / "babata-turn-ledger.jsonl").read_text(encoding="utf-8")
    assert "PROMPT-TAIL" not in raw
    assert "FINAL-TAIL" not in raw
    begin, finish = _jsonl(audit_dir / "babata-turn-ledger.jsonl")
    assert begin["prompt_preview"].endswith("...")
    assert finish["final_preview"].endswith("...")
    assert len(begin["prompt_preview"]) <= turn_audit._MAX_PROMPT_PREVIEW + 3
    assert len(finish["final_preview"]) <= turn_audit._MAX_FINAL_PREVIEW + 3
    assert begin["prompt_sha256"] == turn_audit._sha256_text(long_prompt)
    assert begin["prompt_bytes"] == len(long_prompt.encode("utf-8"))
    assert finish["final_sha256"] == turn_audit._sha256_text(long_final)
    assert finish["final_bytes"] == len(long_final.encode("utf-8"))


def test_turn_audit_caps_error_message(monkeypatch, tmp_path):
    audit_dir = tmp_path / "audit"
    monkeypatch.setenv("BABATA_TURN_LEDGER", "1")
    monkeypatch.setenv("BABATA_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("BABATA_REVIEW_BUS", "off")
    error_message = "error-" + ("e" * 500) + "-ERROR-TAIL"

    turn = turn_audit.begin_turn(
        cpu="codex",
        channel="test",
        prompt="short",
        session_id_before=None,
        cwd=tmp_path,
    )
    summary = turn_audit.finish_turn(turn, error=RuntimeError(error_message))

    assert summary is not None
    raw = (audit_dir / "babata-turn-ledger.jsonl").read_text(encoding="utf-8")
    assert "ERROR-TAIL" not in raw
    finish = _jsonl(audit_dir / "babata-turn-ledger.jsonl")[-1]
    assert finish["error"]["type"] == "RuntimeError"
    assert finish["error"]["message_preview"].endswith("...")
    assert len(finish["error"]["message_preview"]) <= turn_audit._MAX_ERROR_PREVIEW + 3
    assert finish["error"]["message_sha256"] == turn_audit._sha256_text(error_message)
    assert finish["error"]["message_bytes"] == len(error_message.encode("utf-8"))


def test_turn_audit_caps_tool_command(monkeypatch, tmp_path):
    audit_dir = tmp_path / "audit"
    monkeypatch.setenv("BABATA_TURN_LEDGER", "1")
    monkeypatch.setenv("BABATA_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("BABATA_REVIEW_BUS", "off")
    command = "python3 -c " + ("'print(1); " * 80) + "COMMAND-TAIL"

    summary = turn_audit.summarize_tool_use("Bash", {"command": command})
    assert "COMMAND-TAIL" not in summary["command"]
    assert summary["command_sha256"] == turn_audit._sha256_text(command)
    assert summary["command_bytes"] == len(command.encode("utf-8"))

    turn = turn_audit.begin_turn(
        cpu="codex",
        channel="test",
        prompt="short",
        session_id_before=None,
        cwd=tmp_path,
    )
    result = turn_audit.finish_turn(
        turn,
        response=Response(content="done", session_id="sid-1", cost=0.0),
        tool_uses=[{"name": "Bash", "command": command}],
    )

    assert result is not None
    raw = (audit_dir / "babata-turn-ledger.jsonl").read_text(encoding="utf-8")
    assert "COMMAND-TAIL" not in raw
    tool = _jsonl(audit_dir / "babata-turn-ledger.jsonl")[-1]["tool_uses"][0]
    assert tool["command"].endswith("...")
    assert len(tool["command"]) <= turn_audit._MAX_COMMAND_PREVIEW + 3
    assert tool["command_sha256"] == turn_audit._sha256_text(command)
    assert tool["command_bytes"] == len(command.encode("utf-8"))


def test_permission_guard_can_enforce_dangerous_commands(monkeypatch):
    monkeypatch.setenv("BABATA_DETERMINISTIC_GUARDS", "enforce")
    block, reason = turn_audit.should_block_for_permission(
        "Bash",
        {"command": "git reset --hard HEAD"},
    )
    assert block is True
    assert reason and "dangerous-git-command" in reason

    result = asyncio.run(
        cc._always_allow(
            "Bash",
            {"command": "git reset --hard HEAD"},
            ToolPermissionContext(),
        )
    )

    assert result.behavior == "deny"
    assert "dangerous-git-command" in result.message


def test_permission_guard_can_enforce_secret_file_writes(monkeypatch):
    monkeypatch.setenv("BABATA_DETERMINISTIC_GUARDS", "enforce")
    block, reason = turn_audit.should_block_for_permission(
        "Write",
        {
            "file_path": ".env",
            "content": "ANTHROPIC_API_KEY=sk-ant-testsecret0000000000000000",
        },
    )
    assert block is True
    assert reason and "env-file-tool-request" in reason


def test_deterministic_guards_combine_file_and_tool_findings(monkeypatch, tmp_path):
    monkeypatch.setenv("BABATA_DETERMINISTIC_GUARDS", "observe")
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-testsecret0000000000000000\n")

    findings = turn_audit.run_deterministic_guards(
        repo_root=tmp_path,
        changed_files=[".env"],
        tool_uses=[
            {
                "name": "Bash",
                "command": "rm -rf /tmp/babata-state/memory",
            }
        ],
    )

    rules = {finding["rule"] for finding in findings}
    assert {
        "env-file-changed",
        "secret-pattern:anthropic_api_key",
        "destructive-memory-command",
    } <= rules


def test_tool_guards_keep_path_content_command_and_self_ops_rules(monkeypatch):
    monkeypatch.setenv("BABATA_DETERMINISTIC_GUARDS", "observe")

    findings = turn_audit.run_deterministic_guards(
        repo_root=None,
        changed_files=[],
        tool_uses=[
            {
                "name": "Write",
                "file_path": ".env.local",
                "content_has_secret": True,
                "content_has_launchctl": True,
            },
            {
                "name": "Write",
                "file_path": "scripts/self-ops.sh",
                "content_has_launchctl": True,
            },
            {
                "name": "Bash",
                "command": "git clean -fd",
            },
        ],
    )

    rules = {finding["rule"] for finding in findings}
    assert {
        "env-file-tool-request",
        "secret-pattern:tool-input",
        "inline-launchctl-tool-input",
        "ops-boundary-tool-request",
        "dangerous-git-command",
    } <= rules
    self_ops_inline = [
        finding
        for finding in findings
        if finding["rule"] == "inline-launchctl-tool-input"
        and finding.get("path") == "scripts/self-ops.sh"
    ]
    assert self_ops_inline == []


def test_declared_checks_skip_without_config(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.setenv("BABATA_DECLARED_CHECKS", "1")

    results = turn_audit.run_declared_checks(
        repo_root=repo,
        changed_files=["README.md"],
        guard_findings=[],
    )

    assert results == [{"status": "skipped", "reason": "no .babata/checks.json"}]


def test_declared_checks_skip_config_created_during_turn(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    audit_dir = tmp_path / "audit"
    monkeypatch.setenv("BABATA_TURN_LEDGER", "1")
    monkeypatch.setenv("BABATA_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("BABATA_DECLARED_CHECKS", "1")
    monkeypatch.setenv("BABATA_REVIEW_BUS", "off")

    turn = turn_audit.begin_turn(
        cpu="codex",
        channel="test",
        prompt="create config",
        session_id_before=None,
        cwd=repo,
    )
    (repo / ".babata").mkdir()
    (repo / ".babata/checks.json").write_text(json.dumps({
        "checks": [{"name": "unsafe", "command": "false", "when": ["always"]}]
    }))

    summary = turn_audit.finish_turn(
        turn,
        response=Response(content="done", session_id="sid-1", cost=0.0),
    )

    assert summary is not None
    assert summary["declared_checks"] == [
        {"status": "skipped", "reason": "declared checks config changed during turn"}
    ]


def test_declared_checks_report_item_errors_skips_and_failures(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    checks_dir = repo / ".babata"
    checks_dir.mkdir()
    fail_command = f"{sys.executable} -c 'import sys; print(\"bad\"); sys.exit(2)'"
    (checks_dir / "checks.json").write_text(json.dumps({
        "checks": [
            "bad",
            {"name": "missing-command"},
            {"name": "docs-only", "command": "false", "when": ["docs"]},
            {"name": "fail", "command": fail_command, "when": ["always"]},
        ]
    }))
    monkeypatch.setenv("BABATA_DECLARED_CHECKS", "1")

    results = turn_audit.run_declared_checks(
        repo_root=repo,
        changed_files=["script.py"],
        guard_findings=[],
    )

    assert results[0] == {"status": "config_error", "index": 0, "error": "check must be an object"}
    assert results[1] == {"name": "missing-command", "status": "config_error", "error": "command is required"}
    assert results[2] == {"name": "docs-only", "status": "skipped", "reason": "when did not match"}
    assert results[3]["name"] == "fail"
    assert results[3]["status"] == "failed"
    assert results[3]["exit_code"] == 2
    assert "bad" in results[3]["output_tail"]


def test_turn_audit_does_not_attribute_preexisting_dirty_files(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    audit_dir = tmp_path / "audit"
    monkeypatch.setenv("BABATA_TURN_LEDGER", "1")
    monkeypatch.setenv("BABATA_AUDIT_DIR", str(audit_dir))
    monkeypatch.setenv("BABATA_REVIEW_BUS", "queue")

    (repo / "README.md").write_text("dirty before turn\n")
    turn = turn_audit.begin_turn(
        cpu="claude",
        channel="test",
        prompt="no file changes",
        session_id_before=None,
        cwd=repo,
    )
    summary = turn_audit.finish_turn(
        turn,
        response=Response(content="done", session_id="sid-1", cost=0.0),
    )

    assert summary is not None
    ledger = _jsonl(audit_dir / "babata-turn-ledger.jsonl")
    assert ledger[-1]["git"]["changed_files"] == []
    assert summary["review_tasks"] == []
