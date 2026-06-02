from pathlib import Path

import review_health


def test_review_health_ok_when_counterpart_tools_available(monkeypatch, tmp_path):
    cc_worker = tmp_path / "cc-worker"
    codex = tmp_path / "codex"
    cc_worker.write_text("#!/bin/sh\necho cc-worker\n")
    codex.write_text("#!/bin/sh\necho codex 1.0\n")
    cc_worker.chmod(0o755)
    codex.chmod(0o755)
    monkeypatch.setenv("BABATA_CC_WORKER", str(cc_worker))
    monkeypatch.setenv("BABATA_CODEX_REVIEW_CLI", str(codex))
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_INFRA_STRICT", "1")
    monkeypatch.setenv("BABATA_REVIEW_HEALTH_TTL", "0")

    snap = review_health.review_health_snapshot(force=True)

    assert snap["status"] == "ok"
    assert snap["strict"] is True
    assert snap["probes"]["cc_worker"]["ok"] is True
    assert snap["probes"]["codex"]["ok"] is True


def test_review_health_blocks_when_strict_and_tool_missing(monkeypatch, tmp_path):
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\necho codex 1.0\n")
    codex.chmod(0o755)
    monkeypatch.setenv("BABATA_CC_WORKER", str(tmp_path / "missing-cc-worker"))
    monkeypatch.setenv("BABATA_CODEX_REVIEW_CLI", str(codex))
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_INFRA_STRICT", "1")
    monkeypatch.setenv("BABATA_REVIEW_HEALTH_TTL", "0")

    snap = review_health.review_health_snapshot(force=True)

    assert snap["status"] == "block"
    assert snap["probes"]["cc_worker"]["ok"] is False
    assert snap["probes"]["codex"]["ok"] is True


def test_review_health_degrades_when_soft_and_tool_missing(monkeypatch, tmp_path):
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\necho codex 1.0\n")
    codex.chmod(0o755)
    monkeypatch.setenv("BABATA_CC_WORKER", str(tmp_path / "missing-cc-worker"))
    monkeypatch.setenv("BABATA_CODEX_REVIEW_CLI", str(codex))
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_INFRA_STRICT", "0")
    monkeypatch.setenv("BABATA_REVIEW_HEALTH_TTL", "0")

    snap = review_health.review_health_snapshot(force=True)

    assert snap["status"] == "degraded"


def test_review_health_deterministic_only_mode(monkeypatch):
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_AGENT", "deterministic")
    monkeypatch.setenv("BABATA_REVIEW_HEALTH_TTL", "0")

    snap = review_health.review_health_snapshot(force=True)

    assert snap["status"] == "deterministic-only"
    assert snap["counterpart_enabled"] is False
