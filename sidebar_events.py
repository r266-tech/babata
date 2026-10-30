"""babata sidebar fact events plus a bounded diagnostic trace stream.

三线 (translate / chat / proactive / attention) 都 append. chat 进 prompt 时
grep 当前 url 最近事件摘要塞 page_context (= page memory).

High-volume client translation traces are diagnostics, not page-memory facts.
They are written as sampled batches to a separate 50 MiB + one-rotation stream.

Fact stream only: lines do not directly call each other; consumers decide from
the same append-only event log.

Schema (松散, 每条带 ts/url/kind, 余字段按 kind 自由):
  translate_hit       — L1/L2 cache 命中, 不调 LLM
  translate_spawn     — provider 翻 N 段
  translate_done      — provider 完成 (含 spawn_ms 用时)
  translate_fail      — provider 失败 (timeout / parse / HTTP error)
  chat_turn           — sidepanel user message
  proactive_run       — tab-triggered proactive pass (含 url/title)
  viewport            — content script 推 viewport_hashes (当前可见段)
  attention           — content script 推 visibility/sidepanel/idle
"""

import hashlib
import fcntl
import json
import logging
import os
import time
from threading import Lock
from typing import Any

from constants import SIDEBAR_DATA_DIR

log = logging.getLogger(__name__)

EVENTS_DIR = SIDEBAR_DATA_DIR
EVENTS_FILE = EVENTS_DIR / "events.jsonl"
CLIENT_TRACE_FILE = EVENTS_DIR / "client-trace.jsonl"
MAX_FIELD_TEXT = 512
CLIENT_TRACE_FILE_LIMIT_BYTES = 50 * 1024 * 1024
CLIENT_TRACE_MAX_INPUTS = 4096
CLIENT_TRACE_MAX_SAMPLES = 32
CLIENT_TRACE_MAX_FIELDS = 32
CLIENT_TRACE_MAX_KEY_CHARS = 64
CLIENT_TRACE_MAX_LINE_BYTES = 1024 * 1024
_lock = Lock()
_client_trace_lock = Lock()


def append(url: str, kind: str, **fields: Any) -> None:
    """Append one event. Best-effort, log IO errors (静默失败会让 page_memory 假成功)."""
    rec: dict[str, Any] = {
        "ts": int(time.time()),
        "url": url or "",
        "kind": kind,
    }
    rec.update(_bounded_fields(fields))
    try:
        line = json.dumps(rec, ensure_ascii=False) + "\n"
    except (TypeError, ValueError):
        line = json.dumps({"ts": rec["ts"], "url": rec["url"], "kind": kind, "_serialize_error": True}) + "\n"
    try:
        with _lock:
            EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
            lock_path = EVENTS_FILE.with_name("events.lock")
            with lock_path.open("a+") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                with EVENTS_FILE.open("a", encoding="utf-8") as f:
                    f.write(line)
    except OSError as e:
        log.warning("sidebar_events.append failed (kind=%s url=%s): %s", kind, url, e)


def append_client_trace_batch(url: str, traces: list[Any]) -> None:
    """Append one bounded diagnostic record for a client trace batch.

    Repeated trace rows are aggregated before sampling. The diagnostic lane is
    deliberately separate from ``events.jsonl`` so page-memory facts stay
    append-only and high-signal.
    """
    received_n = len(traces)
    processed = traces[:CLIENT_TRACE_MAX_INPUTS]
    groups: dict[str, dict[str, Any]] = {}
    valid_n = 0
    for trace in processed:
        if not isinstance(trace, dict):
            continue
        valid_n += 1
        fields = _normalize_client_trace(trace)
        fingerprint = json.dumps(
            fields,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        group = groups.get(fingerprint)
        if group is None:
            groups[fingerprint] = {"repeat_n": 1, "fields": fields}
        else:
            group["repeat_n"] += 1

    samples = list(groups.values())[:CLIENT_TRACE_MAX_SAMPLES]
    safe_url = _truncate_text(url or "")
    rec: dict[str, Any] = {
        "ts": int(time.time()),
        "url": safe_url,
        "kind": "client_trace_batch",
        "received_n": received_n,
        "processed_n": len(processed),
        "valid_n": valid_n,
        "invalid_n": len(processed) - valid_n,
        "input_truncated_n": received_n - len(processed),
        "unique_n": len(groups),
        "sampled_n": len(samples),
        "unsampled_unique_n": max(0, len(groups) - len(samples)),
        "samples": samples,
    }
    if len(url or "") > MAX_FIELD_TEXT:
        rec["url_sha256"] = _sha256_text(url)
        rec["url_bytes"] = len(url.encode("utf-8", errors="replace"))
    line = _serialize_client_trace_record(rec)
    _append_bounded_client_trace(line)


def _normalize_client_trace(trace: dict[Any, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for raw_key, value in trace.items():
        if raw_key == "txt" or not isinstance(value, (str, int, float, bool)):
            continue
        if len(fields) >= CLIENT_TRACE_MAX_FIELDS:
            break
        key = str(raw_key)[:CLIENT_TRACE_MAX_KEY_CHARS]
        if not key or key in fields:
            continue
        fields[key] = _truncate_text(value) if isinstance(value, str) else value
    return fields


def _serialize_client_trace_record(rec: dict[str, Any]) -> bytes:
    """Keep a single batch record bounded even for hostile extension input."""
    while True:
        data = (
            json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        if len(data) <= CLIENT_TRACE_MAX_LINE_BYTES:
            return data
        samples = rec.get("samples") or []
        if not samples:
            minimal = {
                key: value
                for key, value in rec.items()
                if key not in {"samples", "sampled_n"}
            }
            minimal["sampled_n"] = 0
            minimal["samples"] = []
            minimal["record_truncated"] = True
            return (
                json.dumps(minimal, ensure_ascii=False, separators=(",", ":"))
                + "\n"
            ).encode("utf-8")
        keep = max(0, len(samples) // 2)
        rec["samples"] = samples[:keep]
        rec["sampled_n"] = keep
        rec["unsampled_unique_n"] = max(0, int(rec["unique_n"]) - keep)
        rec["record_truncated"] = True


def _append_bounded_client_trace(data: bytes) -> None:
    """Append diagnostics with a 50 MiB current file and one rotation."""
    if len(data) > CLIENT_TRACE_FILE_LIMIT_BYTES:
        log.warning(
            "sidebar client trace batch skipped: record bytes=%s limit=%s",
            len(data),
            CLIENT_TRACE_FILE_LIMIT_BYTES,
        )
        return
    try:
        with _client_trace_lock:
            CLIENT_TRACE_FILE.parent.mkdir(parents=True, exist_ok=True)
            current_size = CLIENT_TRACE_FILE.stat().st_size if CLIENT_TRACE_FILE.exists() else 0
            if current_size + len(data) > CLIENT_TRACE_FILE_LIMIT_BYTES:
                rotated = CLIENT_TRACE_FILE.with_name(CLIENT_TRACE_FILE.name + ".1")
                os.replace(CLIENT_TRACE_FILE, rotated)
            with CLIENT_TRACE_FILE.open("ab") as handle:
                handle.write(data)
    except OSError as exc:
        log.warning("sidebar client trace append failed: %s", exc)


def _bounded_fields(fields: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields.items():
        if isinstance(value, str):
            out[key] = _truncate_text(value)
            if len(value) > MAX_FIELD_TEXT:
                out[f"{key}_sha256"] = _sha256_text(value)
                out[f"{key}_bytes"] = len(value.encode("utf-8", errors="replace"))
            continue
        out[key] = _bounded_value(value)
    return out


def _bounded_value(value: Any) -> Any:
    if isinstance(value, str):
        return _truncate_text(value)
    if isinstance(value, dict):
        return {str(k): _bounded_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_bounded_value(v) for v in value]
    return value


def _truncate_text(text: str) -> str:
    if "... [truncated " in text and text.endswith(" chars]"):
        return text
    if len(text) <= MAX_FIELD_TEXT:
        return text
    return f"{text[:MAX_FIELD_TEXT]}... [truncated {len(text) - MAX_FIELD_TEXT} chars]"


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _grep_url(url: str, max_records: int = 100, max_age_sec: int = 86400 * 30) -> list[dict]:
    """Return events for given url, oldest first, capped at max_records (most recent N).

    max_age_sec: 默认 30 天内. 超龄事件不返 (page memory 自然衰减).
    """
    if not url:
        return []
    cutoff = int(time.time()) - max_age_sec
    out: list[dict] = []
    try:
        with EVENTS_FILE.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("url") != url:
                    continue
                if int(rec.get("ts", 0)) < cutoff:
                    continue
                out.append(rec)
    except OSError:
        return []
    return out[-max_records:]


def summarize_for_chat(url: str) -> str:
    """Compress events of a url into 1-2 short lines for page_context injection.

    返两段:
      [page_state: ...]   — 当前活态 (最近 5min: viewport / attention)
      [page_memory: ...]  — 历史档案 (跨访问累积)

    都返空 = 第一次看 + 当前没态. LLM 自然不会引用.
    """
    events = _grep_url(url)
    if not events:
        return ""

    now = int(time.time())
    recent_cutoff = now - 300  # 5min 内 = 当前活态
    recent = [e for e in events if int(e.get("ts", 0)) >= recent_cutoff]

    state_line = _format_page_state(recent)
    memory_line = _format_page_memory(events, now)
    return "\n".join(s for s in (state_line, memory_line) if s)


def _format_page_state(recent: list[dict]) -> str:
    """最近 5min 内 viewport / attention. 当前视口几段 / 是不是在看."""
    if not recent:
        return ""
    last_viewport = next(
        (e for e in reversed(recent) if e.get("kind") == "viewport"), None,
    )
    last_attention = next(
        (e for e in reversed(recent) if e.get("kind") == "attention"), None,
    )
    parts: list[str] = []
    if last_viewport:
        n = last_viewport.get("visible_n") or len(last_viewport.get("visible_hashes") or [])
        if n:
            parts.append(f"{n} segments visible")
    if last_attention:
        if last_attention.get("visibility") == "hidden":
            parts.append("tab hidden")
        if last_attention.get("focus") == "no":
            parts.append("window unfocused")
        if last_attention.get("idle") == "yes":
            idle_s = last_attention.get("idle_sec") or 0
            parts.append(f"idle {_humanize_age(int(idle_s))}")
    if not parts:
        return ""
    return "[page_state: " + " | ".join(parts) + "]"


def _format_page_memory(events: list[dict], now: int) -> str:
    last_ts = max(int(e.get("ts", 0)) for e in events)
    age_sec = max(0, now - last_ts)
    age_label = _humanize_age(age_sec)

    n_translate = sum(1 for e in events if e.get("kind", "").startswith("translate_"))
    n_chat = sum(1 for e in events if e.get("kind") == "chat_turn")
    n_proactive = sum(1 for e in events if e.get("kind") == "proactive_run")

    parts = [f"last activity {age_label} ago"]
    if n_translate:
        parts.append(f"{n_translate} translate events")
    if n_chat:
        parts.append(f"{n_chat} chat turns")
    if n_proactive:
        parts.append(f"{n_proactive} proactive runs")

    return "[page_memory: " + " | ".join(parts) + "]"


def _humanize_age(sec: int) -> str:
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"
