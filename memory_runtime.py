"""Shared babata memory inject/reflex runtime helpers."""

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


def memory_inject_script() -> Path:
    configured = os.environ.get("BABATA_MEMORY_INJECT_SCRIPT")
    return Path(configured).expanduser() if configured else _DEFAULT_MEMORY_INJECT_SCRIPT


def memory_reflex_enabled() -> bool:
    return os.environ.get("BABATA_MEMORY_REFLEX", "1") != "0"


def memory_reflex_mode() -> str:
    if not memory_reflex_enabled():
        return "off"
    mode = os.environ.get("BABATA_MEMORY_REFLEX_MODE", "dry-run").strip().lower()
    return mode if mode in {"dry-run", "enforce"} else "dry-run"


def memory_reflex_script() -> Path:
    configured = os.environ.get("BABATA_MEMORY_REFLEX_SCRIPT")
    return Path(configured).expanduser() if configured else _DEFAULT_MEMORY_REFLEX_SCRIPT


def memory_reflex_timeout() -> float:
    raw = os.environ.get("BABATA_MEMORY_REFLEX_TIMEOUT", "0.8")
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 0.8


def default_memory_source() -> str:
    return os.environ.get("BABATA_MEMORY_SOURCE") or "unknown"


def memory_reflex_for_prompt(
    *,
    source: str,
    user_prompt: str | None,
    cpu: str,
    cwd: str,
) -> dict[str, Any]:
    if not memory_reflex_enabled() or not user_prompt:
        return {}
    script = memory_reflex_script()
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
            timeout=memory_reflex_timeout(),
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


def format_memory_reflex_hint(reflex: dict[str, Any]) -> str:
    routes = [str(r) for r in reflex.get("routes", []) if str(r)]
    profile = str(reflex.get("profile") or "lite")
    if not routes or (profile == "lite" and all(r in {"none", "lite"} for r in routes)):
        return ""
    reasons = reflex.get("reasons")
    reason_text = "; ".join(str(r) for r in reasons[:3]) if isinstance(reasons, list) else ""
    return "\n".join([
        "<memory-reflex>",
        f"routes: {', '.join(routes)}",
        f"profile: {profile}",
        "note: router signal only; retrieve deeper evidence only when useful.",
        f"why: {reason_text}" if reason_text else "why: unspecified",
        "</memory-reflex>",
    ])


def _memory_reflex_log_path() -> Path:
    configured = os.environ.get("BABATA_MEMORY_REFLEX_LOG")
    return Path(configured).expanduser() if configured else _DEFAULT_MEMORY_REFLEX_LOG


def _message_summary(text: str | None, limit: int = 180) -> str:
    compact = " ".join((text or "").split())
    return compact[:limit].rstrip()


def _append_memory_reflex_event(payload: dict[str, Any]) -> None:
    try:
        path = _memory_reflex_log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception as exc:
        log.warning("babata memory reflex log failed: %s", exc)


def log_memory_reflex_preflight(
    *,
    reflex: dict[str, Any],
    user_prompt: str | None,
    source: str,
    cpu: str,
    mode: str,
    actual_profile: str,
    memory_injected: bool,
    hint_injected: bool,
) -> str | None:
    if not reflex:
        return None
    now = time.time()
    digest = hashlib.sha256((user_prompt or "").encode("utf-8")).hexdigest()
    event_id = hashlib.sha256(f"{now}:{cpu}:{source}:{digest}".encode("utf-8")).hexdigest()[:16]
    _append_memory_reflex_event({
        "event": "preflight",
        "id": event_id,
        "ts": now,
        "source": source,
        "cpu": cpu,
        "mode": mode,
        "message_sha256": digest,
        "message_summary": _message_summary(user_prompt),
        "router": reflex,
        "actual_profile": actual_profile,
        "memory_injected": memory_injected,
        "hint_injected": hint_injected,
        "post_answer_observation": "pending",
    })
    return event_id


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
        "answer_summary": _message_summary(content),
        "observation": _answer_memory_observation(content or ""),
    })


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
    script = memory_inject_script()
    if not script.is_file():
        log.warning("babata memory inject script missing: %s", script)
        return "", None
    reflex = memory_reflex_for_prompt(
        source=source,
        user_prompt=user_prompt,
        cpu=cpu,
        cwd=cwd,
    )
    mode = memory_reflex_mode()
    enforce = mode == "enforce"
    actual_profile = os.environ.get("BABATA_MEMORY_PROFILE") or (
        str(reflex.get("profile") or "lite") if enforce else "lite"
    )
    env = os.environ.copy()
    env["BABATA_MEMORY_PROFILE"] = actual_profile
    env["BABATA_MEMORY_CPU"] = cpu
    env["BABATA_MEMORY_SOURCE"] = source
    env["BABATA_MEMORY_INCLUDE_TOP"] = "force"
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
        log.warning("babata memory inject failed: %s", exc)
        return "", None
    if result.returncode != 0:
        log.warning(
            "babata memory inject exited %s: %s",
            result.returncode,
            result.stderr.strip()[:500],
        )
        return "", None
    parts = [result.stdout.strip()]
    hint = format_memory_reflex_hint(reflex) if enforce else ""
    if hint:
        parts.append(hint)
    context = "\n\n".join(part for part in parts if part)
    event_id = log_memory_reflex_preflight(
        reflex=reflex,
        user_prompt=user_prompt,
        source=source,
        cpu=cpu,
        mode=mode,
        actual_profile=actual_profile,
        memory_injected=bool(context),
        hint_injected=bool(hint),
    )
    return context, event_id
