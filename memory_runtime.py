"""Shared Babata memory context delivery for channel CPU adapters."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

_DEFAULT_MEMORY_INJECT_SCRIPT = Path.home() / "cc-workspace/scripts/memory-inject.sh"
_MEMORY_INJECT_FLAGS = {
    "claude": "BABATA_CC_MEMORY_INJECT",
    "codex": "BABATA_CODEX_MEMORY_INJECT",
    "grok": "BABATA_GROK_MEMORY_INJECT",
}
_MEMORY_INJECT_TIMEOUTS = {
    "claude": "BABATA_CC_MEMORY_INJECT_TIMEOUT",
    "codex": "BABATA_CODEX_MEMORY_INJECT_TIMEOUT",
    "grok": "BABATA_GROK_MEMORY_INJECT_TIMEOUT",
}


def _memory_inject_script() -> Path:
    configured = os.environ.get("BABATA_MEMORY_INJECT_SCRIPT")
    return Path(configured).expanduser() if configured else _DEFAULT_MEMORY_INJECT_SCRIPT


def default_memory_source() -> str:
    return os.environ.get("BABATA_MEMORY_SOURCE") or "unknown"


def memory_inject_enabled(cpu: str) -> bool:
    if os.environ.get("BABATA_CRON_AGENT") == "1":
        return False
    env_name = _MEMORY_INJECT_FLAGS[cpu]
    return os.environ.get(env_name, "1") != "0"


def memory_inject_timeout(cpu: str) -> float:
    env_name = _MEMORY_INJECT_TIMEOUTS[cpu]
    raw = os.environ.get(env_name, "5")
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 5.0


def _memory_inject_env(*, profile: str, cpu: str, source: str) -> dict[str, str]:
    env = os.environ.copy()
    env["BABATA_MEMORY_PROFILE"] = profile
    env["BABATA_MEMORY_CPU"] = cpu
    env["BABATA_MEMORY_SOURCE"] = source
    env["BABATA_MEMORY_INCLUDE_TOP"] = os.environ.get("BABATA_MEMORY_INCLUDE_TOP") or "skip"
    return env


def _run_memory_inject(script: Path, env: dict[str, str], timeout: float) -> str:
    try:
        result = subprocess.run(
            [str(script)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
        )
    except Exception as exc:
        log.warning("babata memory context render failed: %s", exc)
        return ""
    if result.returncode != 0:
        log.warning(
            "babata memory context render exited %s: %s",
            result.returncode,
            result.stderr.strip()[:500],
        )
        return ""
    return result.stdout.strip()


def render_babata_memory_context(
    *,
    enabled: bool,
    source: str,
    cpu: str,
    timeout: float,
) -> str:
    if not enabled:
        return ""
    script = _memory_inject_script()
    if not script.is_file():
        log.warning("babata memory context script missing: %s", script)
        return ""
    profile = os.environ.get("BABATA_MEMORY_PROFILE", "standing").strip()
    if profile not in {"standing", "lite", "recent", "deep"}:
        profile = "standing"
    return _run_memory_inject(
        script,
        _memory_inject_env(profile=profile, cpu=cpu, source=source),
        timeout,
    )
