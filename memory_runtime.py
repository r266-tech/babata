"""Shared babata memory context/reflex runtime helpers."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_DEFAULT_MEMORY_INJECT_SCRIPT = Path.home() / "cc-workspace/scripts/memory-inject.sh"
_DEFAULT_MEMORY_REFLEX_SCRIPT = Path.home() / "cc-workspace/bin/babata-memory-reflex"
_DEFAULT_MEMORY_REFLEX_LOG = Path.home() / "cc-workspace/state/memory-reflex/events.jsonl"
_MEMORY_INJECT_FLAGS = {
    "claude": "BABATA_CC_MEMORY_INJECT",
    "codex": "BABATA_CODEX_MEMORY_INJECT",
}
_MEMORY_INJECT_TIMEOUTS = {
    "claude": "BABATA_CC_MEMORY_INJECT_TIMEOUT",
    "codex": "BABATA_CODEX_MEMORY_INJECT_TIMEOUT",
}
_REFLEX_ROUTES = {"none", "lite", "brain", "wx", "code-grounded", "recent", "deep"}
_REFLEX_PROFILES = {"lite", "recent", "deep"}
_REFLEX_FLAGS = {"bad_case", "reflection_candidate"}
_REFLEX_VALUE_MAX_CHARS = 40


def _memory_inject_script() -> Path:
    configured = os.environ.get("BABATA_MEMORY_INJECT_SCRIPT")
    return Path(configured).expanduser() if configured else _DEFAULT_MEMORY_INJECT_SCRIPT


def _memory_reflex_enabled() -> bool:
    return os.environ.get("BABATA_MEMORY_REFLEX") == "1"


def memory_reflex_mode() -> str:
    if not _memory_reflex_enabled():
        return "off"
    mode = os.environ.get("BABATA_MEMORY_REFLEX_MODE", "dry-run").strip().lower()
    return mode if mode in {"dry-run", "enforce"} else "dry-run"


def _memory_reflex_script() -> Path:
    configured = os.environ.get("BABATA_MEMORY_REFLEX_SCRIPT")
    return Path(configured).expanduser() if configured else _DEFAULT_MEMORY_REFLEX_SCRIPT


def _memory_reflex_timeout() -> float:
    raw = os.environ.get("BABATA_MEMORY_REFLEX_TIMEOUT", "0.8")
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 0.8


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


def _memory_reflex_for_prompt(
    *,
    source: str,
    user_prompt: str | None,
    cpu: str,
    cwd: str,
) -> dict[str, Any]:
    if not _memory_reflex_enabled() or not user_prompt:
        return {}
    script = _memory_reflex_script()
    if not script.is_file():
        log.warning("babata memory reflex script missing: %s", script)
        return {}
    try:
        result = subprocess.run(
            [
                str(script),
                "--message", "-",
                "--source", source,
                "--cpu", cpu,
                "--cwd", cwd,
            ],
            input=user_prompt,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=_memory_reflex_timeout(),
            check=False,
        )
    except Exception as exc:
        log.warning("babata memory reflex failed: %s", exc)
        return {}
    if result.returncode != 0:
        log.warning("babata memory reflex exited %s: %s", result.returncode, result.stderr.strip()[:500])
        return {}
    try:
        parsed = json.loads(result.stdout)
    except Exception as exc:
        log.warning("babata memory reflex returned invalid json: %s", exc)
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _sanitize_reflex_token(value: Any, allowed: set[str]) -> str | None:
    token = str(value or "").strip()
    if len(token) > _REFLEX_VALUE_MAX_CHARS:
        return None
    return token if token in allowed else None


def _sanitize_reflex_list(value: Any, allowed: set[str]) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        token = _sanitize_reflex_token(item, allowed)
        if token and token not in out:
            out.append(token)
    return out


def _memory_reflex_log_path() -> Path:
    configured = os.environ.get("BABATA_MEMORY_REFLEX_LOG")
    return Path(configured).expanduser() if configured else _DEFAULT_MEMORY_REFLEX_LOG


def _append_memory_reflex_event(payload: dict[str, Any]) -> None:
    try:
        path = _memory_reflex_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:
        log.warning("babata memory reflex log failed: %s", exc)


def _log_memory_reflex_preflight(
    *,
    reflex: dict[str, Any],
    user_prompt: str | None,
    source: str,
    cpu: str,
    mode: str,
    actual_profile: str,
    memory_injected: bool,
) -> str | None:
    if not reflex:
        return None
    now = time.time()
    digest = hashlib.sha256((user_prompt or "").encode("utf-8")).hexdigest()
    event_id = hashlib.sha256(f"{now}:{cpu}:{source}:{digest}".encode("utf-8")).hexdigest()[:16]
    router: dict[str, Any] = {}
    routes = _sanitize_reflex_list(reflex.get("routes"), _REFLEX_ROUTES)
    flags = _sanitize_reflex_list(reflex.get("flags"), _REFLEX_FLAGS)
    profile = _sanitize_reflex_token(reflex.get("profile"), _REFLEX_PROFILES)
    if routes:
        router["routes"] = routes
    if flags:
        router["flags"] = flags
    if profile:
        router["profile"] = profile
    _append_memory_reflex_event({
        "event": "preflight",
        "id": event_id,
        "ts": now,
        "source": source,
        "cpu": cpu,
        "mode": mode,
        "message_sha256": digest,
        "router": router,
        "actual_profile": actual_profile,
        "memory_injected": memory_injected,
        "post_answer_observation": "pending",
    })
    return event_id


def log_memory_reflex_preflight_only(
    *,
    source: str,
    user_prompt: str | None,
    cpu: str,
    cwd: str,
) -> str | None:
    if os.environ.get("BABATA_CRON_AGENT") == "1":
        return None
    reflex = _memory_reflex_for_prompt(
        source=source,
        user_prompt=user_prompt,
        cpu=cpu,
        cwd=cwd,
    )
    return _log_memory_reflex_preflight(
        reflex=reflex,
        user_prompt=user_prompt,
        source=source,
        cpu=cpu,
        mode=memory_reflex_mode(),
        actual_profile=os.environ.get("BABATA_MEMORY_PROFILE") or "lite",
        memory_injected=False,
    )


def _answer_memory_observation(content: str) -> dict[str, Any]:
    markers = ("不记得", "没记住", "没有记忆", "没有记录", "查不到", "没查到", "无法确认", "没有找到")
    return {
        "heuristic_only": True,
        "memory_miss_marker": any(marker in content for marker in markers),
        "wrong_recall": None,
        "missed_required_lookup": None,
    }


def log_memory_reflex_post_answer(event_id: str | None, content: str) -> None:
    if not event_id:
        return
    _append_memory_reflex_event({
        "event": "post_answer",
        "id": event_id,
        "ts": time.time(),
        "answer_sha256": hashlib.sha256((content or "").encode("utf-8")).hexdigest(),
        "observation": _answer_memory_observation(content or ""),
    })


def _memory_context_profile(reflex: dict[str, Any], mode: str) -> str:
    configured = os.environ.get("BABATA_MEMORY_PROFILE")
    if configured:
        return _sanitize_reflex_token(configured, _REFLEX_PROFILES) or "lite"
    if mode == "enforce":
        return _sanitize_reflex_token(reflex.get("profile"), _REFLEX_PROFILES) or "lite"
    return "lite"


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


def render_babata_memory_context_event(
    *,
    enabled: bool,
    source: str,
    user_prompt: str | None,
    cpu: str,
    cwd: str,
    timeout: float,
) -> tuple[str, str | None]:
    if not enabled:
        return "", None
    script = _memory_inject_script()
    if not script.is_file():
        log.warning("babata memory context script missing: %s", script)
        return "", None
    reflex = _memory_reflex_for_prompt(
        source=source,
        user_prompt=user_prompt,
        cpu=cpu,
        cwd=cwd,
    )
    mode = memory_reflex_mode()
    actual_profile = _memory_context_profile(reflex, mode)
    context_text = _run_memory_inject(
        script,
        _memory_inject_env(profile=actual_profile, cpu=cpu, source=source),
        timeout,
    )
    if not context_text:
        return "", None
    event_id = _log_memory_reflex_preflight(
        reflex=reflex,
        user_prompt=user_prompt,
        source=source,
        cpu=cpu,
        mode=mode,
        actual_profile=actual_profile,
        memory_injected=bool(context_text),
    )
    return context_text, event_id
