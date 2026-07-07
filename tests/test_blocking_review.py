import asyncio
import json
import subprocess
import sys
from pathlib import Path

import codex_engine
import cc
import blocking_review
from claude_agent_sdk import ResultMessage

from test_codex_engine import FakeProcess, _json_line
from test_live_session import FakeClaudeSDKClient, wait_for


IDENTITY_PROMPT_MARKERS = (
    "babata's",
    "You are babata",
    "你是 babata",
    "共同进化",
    "身份认同",
)


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=path, check=True)
    (path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=path, check=True, capture_output=True)


def _review_cmd(tmp_path: Path) -> str:
    script = tmp_path / "review_cmd.py"
    script.write_text(
        "import json, sys\n"
        "payload = json.load(sys.stdin)\n"
        "if payload['round_index'] == 0:\n"
        "    print(json.dumps({'status':'needs_fix','findings':[{'severity':'high','rule':'unit-review','message':'fix it'}]}))\n"
        "else:\n"
        "    print(json.dumps({'status':'passed'}))\n"
    )
    return f"{sys.executable} {script}"


def _codex_lines(sid: str, text: str) -> list[str]:
    return [
        _json_line({"type": "thread.started", "thread_id": sid}),
        _json_line({
            "type": "item.completed",
            "item": {"id": "item_0", "type": "agent_message", "text": text},
        }),
        _json_line({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}),
    ]


def test_codex_query_blocks_for_review_and_repairs_same_task(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.setenv("BABATA_TURN_LEDGER", "1")
    monkeypatch.setenv("BABATA_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("BABATA_DECLARED_CHECKS", "0")
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_CMD", _review_cmd(tmp_path))
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_MAX_ROUNDS", "1")
    monkeypatch.setattr(codex_engine, "_codex_cwd", lambda _source=None: str(repo))
    calls = {"n": 0}

    async def fake_create(*_cmd, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            (repo / "app.py").write_text("print('draft')\n")
            return FakeProcess(_codex_lines("sid-review", "draft"))
        (repo / "app.py").write_text("print('fixed')\n")
        return FakeProcess(_codex_lines("sid-review", "fixed"))

    async def run():
        monkeypatch.setattr(codex_engine.asyncio, "create_subprocess_exec", fake_create)
        session = codex_engine.CodexEngine(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)

        resp = await session.query("change code")

        assert resp.content == "fixed"
        assert calls["n"] == 2
        assert resp.audit["blocking_review"]["status"] == "passed"

    asyncio.run(run())


def test_claude_live_review_followup_precedes_turn_end(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    monkeypatch.setenv("BABATA_TURN_LEDGER", "1")
    monkeypatch.setenv("BABATA_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("BABATA_DECLARED_CHECKS", "0")
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_CMD", _review_cmd(tmp_path))
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_MAX_ROUNDS", "1")
    monkeypatch.setattr(cc, "_DEFAULT_CWD", str(repo))

    async def run():
        FakeClaudeSDKClient.instances.clear()
        monkeypatch.setattr(cc, "ClaudeSDKClient", FakeClaudeSDKClient)
        session = cc.LiveSession(
            state_file=tmp_path / "session.json",
            source_prompt="Source: test.",
        )
        monkeypatch.setattr(session, "_fire_hook", lambda *_: None)

        await session.connect()
        client = FakeClaudeSDKClient.instances[-1]
        session.submit("change code")
        await wait_for(lambda: len(client.sent) == 1)

        agen = session.events()
        (repo / "app.py").write_text("print('draft')\n")
        client.receive_queue.put_nowait(ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sid-live",
            result="draft",
        ))

        first = await agen.__anext__()
        assert first.kind == "session_changed"
        await wait_for(lambda: len(client.sent) == 2)
        assert "<blocking-review>" in client.sent[1]["message"]["content"]

        (repo / "app.py").write_text("print('fixed')\n")
        client.receive_queue.put_nowait(ResultMessage(
            subtype="success",
            duration_ms=1,
            duration_api_ms=1,
            is_error=False,
            num_turns=1,
            session_id="sid-live",
            result="fixed",
        ))
        final = await agen.__anext__()
        await agen.aclose()
        await session.close()

        assert final.kind == "turn_end"
        assert final.response.content == "fixed"
        assert final.response.audit["blocking_review"]["status"] == "passed"

    asyncio.run(run())


def test_codex_turn_routes_default_review_to_claude_counterpart(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "app.py").write_text("print('draft')\n")
    calls = []

    class Proc:
        returncode = 0
        stdout = json.dumps({
            "initial_turn": {
                "exit_code": 0,
                "timed_out": False,
                "text": json.dumps({"status": "needs_fix", "findings": [
                    {"severity": "high", "rule": "unit", "message": "bug", "path": "app.py"}
                ]}),
            }
        })
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return Proc()

    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_COUNTERPART", "1")
    monkeypatch.setenv("BABATA_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(blocking_review, "_cc_worker_cli", lambda: Path("/bin/cc-worker"))
    monkeypatch.setattr(blocking_review.subprocess, "run", fake_run)

    result = blocking_review.run_blocking_review(
        {
            "turn_id": "turn-1",
            "repo_root": str(repo),
            "changed_files": ["app.py"],
            "declared_checks": [{"status": "skipped"}],
        },
        cpu="codex",
        channel="test",
        response_content="draft",
        round_index=0,
    )

    assert result["status"] == "needs_fix"
    assert result["reviewer"] == "claude-counterpart"
    start_call = next(call for call in calls if call[0][0] == "/bin/cc-worker" and call[0][1] == "start")
    assert start_call[0][0:2] == ["/bin/cc-worker", "start"]
    assert "--role" in start_call[0]
    assert "review" in start_call[0]
    assert start_call[1]["env"]["BABATA_BLOCKING_REVIEW"] == "0"
    assert start_call[1]["env"]["BABATA_BLOCKING_REVIEW_DEPTH"] == "1"
    assert any(call[0][0] == "/bin/cc-worker" and call[0][1] == "remove" for call in calls)


def test_claude_turn_routes_default_review_to_codex_counterpart(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "app.py").write_text("print('draft')\n")
    calls = []

    class Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        output_flag = "--output-last-message"
        if output_flag in args:
            output_path = Path(args[args.index(output_flag) + 1])
            output_path.write_text(json.dumps({"status": "passed", "findings": []}))
        return Proc()

    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_COUNTERPART", "1")
    monkeypatch.setenv("BABATA_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setattr(blocking_review, "_codex_cli", lambda: Path("/bin/codex"))
    monkeypatch.setattr(blocking_review.subprocess, "run", fake_run)

    result = blocking_review.run_blocking_review(
        {
            "turn_id": "turn-2",
            "repo_root": str(repo),
            "changed_files": ["app.py"],
            "declared_checks": [{"status": "skipped"}],
        },
        cpu="claude",
        channel="test",
        response_content="draft",
        round_index=0,
    )

    assert result["status"] == "passed"
    assert result["reviewer"] == "codex-counterpart"
    codex_call = next(call for call in calls if call[0][0] == "/bin/codex" and call[0][1] == "exec")
    assert "--sandbox" in codex_call[0]
    assert "read-only" in codex_call[0]
    assert codex_call[1]["env"]["BABATA_BLOCKING_REVIEW"] == "0"
    assert codex_call[1]["env"]["BABATA_BLOCKING_REVIEW_DEPTH"] == "1"


def test_counterpart_review_skips_when_already_inside_reviewer(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "app.py").write_text("print('draft')\n")
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_DEPTH", "1")
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_MAX_DEPTH", "1")
    monkeypatch.setenv("BABATA_AUDIT_DIR", str(tmp_path / "audit"))

    result = blocking_review.run_blocking_review(
        {"turn_id": "turn-3", "repo_root": str(repo), "changed_files": ["app.py"]},
        cpu="codex",
        channel="test",
        response_content="draft",
        round_index=0,
    )

    assert result["status"] == "passed"
    assert result["reviewer"] == "deterministic"
    assert result["reason"] == "counterpart review skipped inside delegated reviewer"


def test_repair_prompt_stays_compact_without_losing_repair_boundary():
    prompt = blocking_review.build_repair_prompt({
        "findings": [
            {
                "severity": "high",
                "rule": "unit",
                "path": "app.py",
                "message": "fix regression",
            }
        ]
    })

    assert len(prompt) <= 360
    for marker in (
        "<blocking-review>",
        "same repository/session",
        "Do not ask implementation details",
        "Rerun relevant checks",
        "fix regression",
    ):
        assert marker in prompt
    for marker in ("previous code-changing turn did not pass", "confirm implementation details"):
        assert marker not in prompt
    for marker in IDENTITY_PROMPT_MARKERS:
        assert marker not in prompt


def test_counterpart_review_prompt_stays_compact_without_losing_review_boundary(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "app.py").write_text("print('changed')\n")
    payload = {
        "cpu": "codex",
        "audit": {
            "turn_id": "turn-compact",
            "repo_root": str(repo),
            "changed_files": ["app.py"],
            "declared_checks": [{"name": "unit", "status": "passed"}],
        },
        "response_preview": "changed app.py",
    }

    prompt = blocking_review._build_counterpart_review_prompt(payload, reviewer="claude")

    assert len(prompt) <= 1400
    for marker in (
        "synchronous blocking review gate",
        "Read-only sidecar",
        "Return JSON only",
        "Block only concrete bugs",
        "Do not block style",
        "Audit summary:",
        '"changed_files":["app.py"]',
        "Review context:",
    ):
        assert marker in prompt
    for marker in (
        "This is a bounded sidecar review only",
        "The source CPU will apply any fixes",
        "Return only a JSON object",
        "Changed files:",
        "missing nice-to-have tests",
    ):
        assert marker not in prompt
    for marker in IDENTITY_PROMPT_MARKERS:
        assert marker not in prompt
