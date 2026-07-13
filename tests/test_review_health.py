import review_health


def test_review_health_ok_when_default_codex_reviewer_available(monkeypatch, tmp_path):
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\necho codex 1.0\n")
    codex.chmod(0o755)
    monkeypatch.setenv("BABATA_CC_WORKER", str(tmp_path / "missing-cc-worker"))
    monkeypatch.setenv("BABATA_CODEX_REVIEW_CLI", str(codex))
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_INFRA_STRICT", "1")
    monkeypatch.setenv("BABATA_REVIEW_HEALTH_TTL", "0")

    snap = review_health.review_health_snapshot(force=True)

    assert snap["status"] == "ok"
    assert snap["strict"] is True
    assert snap["review_cpu"] == "codex"
    assert snap["probes"]["codex"]["ok"] is True
    assert "cc_worker" not in snap["probes"]


def test_review_health_blocks_when_strict_and_default_codex_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("BABATA_CODEX_REVIEW_CLI", str(tmp_path / "missing-codex"))
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_INFRA_STRICT", "1")
    monkeypatch.setenv("BABATA_REVIEW_HEALTH_TTL", "0")

    snap = review_health.review_health_snapshot(force=True)

    assert snap["status"] == "block"
    assert snap["probes"]["codex"]["ok"] is False


def test_review_health_degrades_when_soft_and_tool_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("BABATA_CODEX_REVIEW_CLI", str(tmp_path / "missing-codex"))
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_INFRA_STRICT", "0")
    monkeypatch.setenv("BABATA_REVIEW_HEALTH_TTL", "0")

    snap = review_health.review_health_snapshot(force=True)

    assert snap["status"] == "degraded"


def test_review_health_explicit_claude_reviewer_probes_only_cc_worker(monkeypatch, tmp_path):
    cc_worker = tmp_path / "cc-worker"
    cc_worker.write_text("#!/bin/sh\necho cc-worker\n")
    cc_worker.chmod(0o755)
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_CPU", "claude")
    monkeypatch.setenv("BABATA_CC_WORKER", str(cc_worker))
    monkeypatch.setenv("BABATA_CODEX_REVIEW_CLI", str(tmp_path / "missing-codex"))
    monkeypatch.setenv("BABATA_REVIEW_HEALTH_TTL", "0")

    snap = review_health.review_health_snapshot(force=True)

    assert snap["status"] == "ok"
    assert snap["review_cpu"] == "claude"
    assert snap["probes"]["cc_worker"]["ok"] is True
    assert "codex" not in snap["probes"]


def test_review_health_unknown_cpu_falls_back_to_codex_probe(monkeypatch, tmp_path):
    codex = tmp_path / "codex"
    codex.write_text("#!/bin/sh\necho codex 1.0\n")
    codex.chmod(0o755)
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_CPU", "cluade")
    monkeypatch.setenv("BABATA_CODEX_REVIEW_CLI", str(codex))
    monkeypatch.setenv("BABATA_CC_WORKER", str(tmp_path / "missing-cc-worker"))
    monkeypatch.setenv("BABATA_REVIEW_HEALTH_TTL", "0")

    snap = review_health.review_health_snapshot(force=True)

    assert snap["status"] == "ok"
    assert snap["configured_review_cpu"] == "cluade"
    assert snap["review_cpu"] == "codex"
    assert snap["probes"]["codex"]["ok"] is True
    assert "cc_worker" not in snap["probes"]


def test_review_health_deterministic_only_mode(monkeypatch):
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW_AGENT", "deterministic")
    monkeypatch.setenv("BABATA_REVIEW_HEALTH_TTL", "0")
    monkeypatch.setattr(
        review_health,
        "_run_probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("probe should not run")),
    )

    snap = review_health.review_health_snapshot(force=True)

    assert snap["status"] == "deterministic-only"
    assert snap["counterpart_enabled"] is False
    assert snap["probes"] == {}


def test_review_health_disabled_skips_external_probes(monkeypatch):
    monkeypatch.setenv("BABATA_BLOCKING_REVIEW", "0")
    monkeypatch.setenv("BABATA_REVIEW_HEALTH_TTL", "0")
    monkeypatch.setattr(
        review_health,
        "_run_probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("probe should not run")),
    )

    snap = review_health.review_health_snapshot(force=True)

    assert snap["status"] == "disabled"
    assert snap["probes"] == {}


def test_review_health_no_probe_reports_not_checked_without_external_calls(monkeypatch):
    monkeypatch.setenv("BABATA_REVIEW_HEALTH_TTL", "0")
    monkeypatch.setattr(
        review_health,
        "_run_probe",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("probe should not run")),
    )

    snap = review_health.review_health_snapshot(force=True, probe=False)

    assert snap["status"] == "not-checked"
    assert snap["enabled"] is True
    assert snap["counterpart_enabled"] is True
    assert snap["probes"] == {}
