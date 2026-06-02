"""Lightweight health probes for babata's blocking review gate."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


_CACHE: tuple[float, dict[str, Any]] | None = None


def review_health_snapshot(*, force: bool = False) -> dict[str, Any]:
    ttl = _cache_ttl_seconds()
    now = time.time()
    global _CACHE
    if not force and _CACHE is not None and now - _CACHE[0] < ttl:
        return _CACHE[1]
    snapshot = _compute_snapshot()
    _CACHE = (now, snapshot)
    return snapshot


def review_health_status() -> str:
    snapshot = review_health_snapshot()
    return str(snapshot.get("status") or "unknown")


def _compute_snapshot() -> dict[str, Any]:
    enabled = os.environ.get("BABATA_BLOCKING_REVIEW", "1") != "0"
    counterpart_enabled = _counterpart_enabled()
    strict = os.environ.get("BABATA_BLOCKING_REVIEW_INFRA_STRICT", "0") == "1"
    probes = {
        "cc_worker": _probe_cc_worker(),
        "codex": _probe_codex(),
    }
    ok = all(item.get("ok") for item in probes.values())
    if not enabled:
        status = "disabled"
    elif not counterpart_enabled:
        status = "deterministic-only"
    elif ok:
        status = "ok"
    elif strict:
        status = "block"
    else:
        status = "degraded"
    return {
        "status": status,
        "enabled": enabled,
        "counterpart_enabled": counterpart_enabled,
        "strict": strict,
        "probes": probes,
        "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


def _counterpart_enabled() -> bool:
    mode = os.environ.get("BABATA_BLOCKING_REVIEW_AGENT", "counterpart").strip().lower()
    if mode in {"0", "off", "false", "disabled", "none", "deterministic"}:
        return False
    return os.environ.get("BABATA_BLOCKING_REVIEW_COUNTERPART", "1") != "0"


def _probe_cc_worker() -> dict[str, Any]:
    cli = _cc_worker_cli()
    if cli is None:
        return {"ok": False, "reason": "cc-worker not found"}
    if os.environ.get("BABATA_REVIEW_HEALTH_DEEP", "0") == "1":
        return _run_probe("cc-worker verify", [str(cli), "verify", "--timeout", "20"], timeout=30)
    result = _run_probe("cc-worker help", [str(cli), "--help"], timeout=5)
    result["path"] = str(cli)
    return result


def _probe_codex() -> dict[str, Any]:
    cli = _codex_cli()
    if cli is None:
        return {"ok": False, "reason": "codex not found"}
    result = _run_probe("codex version", [str(cli), "--version"], timeout=5)
    result["path"] = str(cli)
    return result


def _run_probe(name: str, args: list[str], *, timeout: float) -> dict[str, Any]:
    started = time.time()
    env = dict(os.environ)
    env.setdefault("CODEX_SKIP_AUTO_UPGRADE", "1")
    env["BABATA_BLOCKING_REVIEW"] = "0"
    try:
        proc = subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as e:
        return {
            "ok": False,
            "name": name,
            "reason": f"timeout after {timeout:g}s",
            "output": _trim("\n".join(str(part) for part in (e.stdout, e.stderr) if part), 500),
        }
    except Exception as e:
        return {"ok": False, "name": name, "reason": f"{type(e).__name__}: {e}"}
    output = "\n".join(part for part in (proc.stdout, proc.stderr) if part)
    return {
        "ok": proc.returncode == 0,
        "name": name,
        "exit_code": proc.returncode,
        "duration_ms": round((time.time() - started) * 1000),
        "output": _trim(output.strip(), 500),
    }


def _cc_worker_cli() -> Path | None:
    configured = os.environ.get("BABATA_CC_WORKER")
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() and os.access(path, os.X_OK) else None
    candidates = [
        Path.home() / "cc-workspace/bin/cc-worker",
        Path("~/cc-workspace/bin/cc-worker").expanduser(),
    ]
    for candidate in candidates:
        if candidate and candidate.exists() and os.access(candidate, os.X_OK):
            return candidate
    found = shutil.which("cc-worker")
    return Path(found) if found else None


def _codex_cli() -> Path | None:
    configured = (
        os.environ.get("BABATA_CODEX_REVIEW_CLI")
        or os.environ.get("BABATA_CODEX_CLI_PATH")
        or os.environ.get("CODEX_CLI_PATH")
    )
    if configured:
        path = Path(configured).expanduser()
        return path if path.exists() and os.access(path, os.X_OK) else None
    found = shutil.which("codex")
    return Path(found) if found else None


def _cache_ttl_seconds() -> float:
    raw = os.environ.get("BABATA_REVIEW_HEALTH_TTL", "60")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 60.0


def _trim(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."
