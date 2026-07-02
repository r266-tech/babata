"""Codex CLI engine adapter for babata.

This is intentionally a sibling of ``cc.py`` rather than a rewrite of the
transport layer. TG/WeChat keep their existing channel semantics; this module
only swaps the CPU behind the same small Response/Event surface.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import tempfile
import time
from contextlib import suppress
from pathlib import Path
from typing import Any, AsyncIterator

from blocking_review import (
    blocking_review_max_rounds,
    build_repair_prompt,
    run_blocking_review,
    unresolved_review_message,
)
from constants import HOOKS_DIR as _HOOKS_DIR
from cc import CC, Event, Response, StreamCB, _record_session_metadata
from memory_runtime import (
    log_memory_reflex_preflight_only,
    log_memory_reflex_post_answer,
    memory_reflex_mode,
    render_babata_memory_context_event,
)
from skill_evolve_nudge import notify_skill_evolve_turn
from turn_audit import begin_turn, finish_turn, summarize_tool_use

log = logging.getLogger(__name__)

_CODEX_SESSIONS_KEY = "codex_sessions"
_CODEX_RECENT_LIMIT = 200
_CODEX_IMAGE_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
}
_CODEX_SPLIT_ERRORS = (
    "Separator is not found, and chunk exceed the limit",
    "Separator is found, but chunk is longer than limit",
)
_CODEX_STREAM_LIMIT = 64 * 1024 * 1024
_CODEX_TOOL_RESULT_MAX_CHARS = 4000
_CODEX_MEMORY_INJECTED_KEY = "codex_memory_injected_sids"


def _codex_stall_timeout() -> float:
    raw = os.environ.get("BABATA_CODEX_STALL_TIMEOUT", "1800")
    try:
        value = float(raw)
    except ValueError:
        return 1800.0
    return max(0.0, value)


def _is_codex_split_error(message: str | None) -> bool:
    if not message:
        return False
    return any(marker in message for marker in _CODEX_SPLIT_ERRORS)


def _split_error_content(content: str) -> str:
    if content:
        return content
    return (
        "Codex CLI hit its long-output splitter limit before producing a final "
        "answer. Please retry the request with a narrower scope, or start a "
        "fresh session with /new."
    )


def _codex_cli_path() -> str:
    return (
        os.environ.get("BABATA_CODEX_CLI_PATH")
        or os.environ.get("CODEX_CLI_PATH")
        or "codex"
    )


def _codex_sandbox() -> str:
    configured = os.environ.get("BABATA_CODEX_SANDBOX")
    if configured:
        return configured
    return (
        "danger-full-access"
        if os.environ.get("BABATA_FULL_TRUST") == "1"
        else "workspace-write"
    )


def _codex_cwd(source: str | None = None) -> str:
    if source == "sidebar":
        return str(Path(__file__).parent)
    if os.environ.get("BABATA_FULL_TRUST") == "1":
        return str(Path.home())
    return str(Path(__file__).parent)


def _codex_memory_inject_enabled() -> bool:
    return os.environ.get("BABATA_CODEX_MEMORY_INJECT", "1") != "0"


def _memory_inject_timeout() -> float:
    raw = os.environ.get("BABATA_CODEX_MEMORY_INJECT_TIMEOUT", "5")
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 5.0


def _render_babata_memory_context_event(
    source: str | None = None,
    user_prompt: str | None = None,
) -> tuple[str, str | None]:
    source_name = source or "unknown"
    return render_babata_memory_context_event(
        enabled=_codex_memory_inject_enabled(),
        source=source_name,
        user_prompt=user_prompt,
        cpu="codex",
        cwd=_codex_cwd(source_name),
        timeout=_memory_inject_timeout(),
    )


def _toml_key(key: str) -> str:
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", key):
        return key
    return json.dumps(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    if isinstance(value, dict):
        items = ", ".join(
            f"{_toml_key(str(k))} = {_toml_value(v)}"
            for k, v in value.items()
        )
        return "{ " + items + " }"
    raise TypeError(f"unsupported TOML value: {value!r}")


def _codex_mcp_overrides(mcp_servers: dict[str, Any]) -> list[str]:
    args: list[str] = []
    for name, cfg in (mcp_servers or {}).items():
        if not isinstance(cfg, dict) or not cfg.get("command"):
            continue
        table: dict[str, Any] = {
            "command": str(cfg["command"]),
        }
        if cfg.get("args"):
            table["args"] = [str(a) for a in cfg["args"]]
        if cfg.get("env"):
            table["env"] = {str(k): str(v) for k, v in dict(cfg["env"]).items()}
        if cfg.get("cwd"):
            table["cwd"] = str(cfg["cwd"])
        args.extend(["-c", f"mcp_servers.{name}={_toml_value(table)}"])
    return args


def _decode_image_to_file(img: dict[str, str], directory: Path) -> Path:
    media_type = img.get("media_type") or "image/png"
    suffix = _CODEX_IMAGE_EXT.get(media_type, ".img")
    fp = directory / f"image-{time.time_ns()}{suffix}"
    fp.write_bytes(base64.b64decode(img["data"]))
    return fp


def _extract_tool_name(item: dict[str, Any]) -> str | None:
    item_type = str(item.get("type") or "")
    if item_type == "command_execution":
        command = str(item.get("command") or "shell")
        return command.split(None, 1)[0] if command else "shell"
    if item_type.endswith("tool_call_output") or item_type in {"mcp_call_output", "function_call_output"}:
        name = item.get("name") or item.get("tool_name")
        return str(name) if name else None
    if item_type.endswith("tool_call") or item_type in {"mcp_call", "function_call"}:
        tool = item.get("tool")
        server = item.get("server")
        if tool and server:
            return f"{server}.{tool}"
        if tool:
            return str(tool)
        return str(item.get("name") or item.get("tool_name") or item_type)
    return None


def _extract_tool_id(item: dict[str, Any]) -> str | None:
    raw = item.get("call_id") or item.get("tool_call_id") or item.get("id")
    return str(raw) if raw else None


def _tool_result_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, default=str)
        except Exception:
            text = str(value)
    if len(text) > _CODEX_TOOL_RESULT_MAX_CHARS:
        return text[:_CODEX_TOOL_RESULT_MAX_CHARS].rstrip() + "..."
    return text


def _extract_tool_result(item: dict[str, Any]) -> dict[str, Any]:
    is_error = bool(item.get("is_error") or item.get("error"))
    for key in ("output", "result", "text", "content"):
        value = item.get(key)
        if value not in (None, ""):
            return {"is_error": is_error, "text": _tool_result_text(value)}
    if item.get("error"):
        return {"is_error": True, "text": _tool_result_text(item.get("error"))}
    return {"is_error": is_error, "text": ""}


def _codex_tool_summary(name: str, item: dict[str, Any]) -> dict[str, Any]:
    item_type = str(item.get("type") or "")
    if item_type == "command_execution":
        command = str(item.get("command") or "")
        return summarize_tool_use(
            name,
            {"command": command} if command else {},
        )

    tool_input: dict[str, Any] = {}
    for key in ("arguments", "input", "params"):
        value = item.get(key)
        if isinstance(value, dict):
            tool_input.update(value)
        elif isinstance(value, str):
            try:
                decoded = json.loads(value)
            except Exception:
                tool_input[key] = value
            else:
                if isinstance(decoded, dict):
                    tool_input.update(decoded)
                else:
                    tool_input[key] = value
    if not tool_input and item_type:
        tool_input["type"] = item_type
    return summarize_tool_use(name, tool_input)


class CodexEngine(CC):
    """One-shot Codex CLI backend with babata-compatible state."""

    supports_hot_input = False

    def __init__(
        self,
        *,
        state_file: Path,
        source_prompt: str,
        mcp_servers: dict[str, Any] | None = None,
        memory_source: str | None = None,
    ) -> None:
        super().__init__(
            state_file=state_file,
            source_prompt=source_prompt,
            mcp_servers=mcp_servers,
            memory_source=memory_source,
        )
        self._memory_reflex_event_id: str | None = None

    async def query(
        self,
        prompt: str,
        images: list[dict[str, str]] | None = None,
        on_stream: StreamCB | None = None,
    ) -> Response:
        if prompt.strip() == "/new" and not images:
            self.reset()
            return Response(content="会话已重置。", session_id="", cost=0.0)

        self._check_idle_reset()
        audit = begin_turn(
            cpu="codex",
            channel=self._channel_label(),
            prompt=prompt,
            session_id_before=self._session_id,
            cwd=_codex_cwd(self._memory_source),
            images_count=len(images or []),
        )
        try:
            resp = await self._run_codex(prompt, images, on_stream)
        except Exception as e:
            finish_turn(audit, error=e)
            raise
        resp.audit = finish_turn(
            audit,
            response=resp,
            tools=resp.tools,
            tool_uses=resp.audit.get("tool_uses", []) if isinstance(resp.audit, dict) else [],
        )
        return await self._apply_blocking_review_gate(resp)

    async def _apply_blocking_review_gate(self, resp: Response) -> Response:
        channel = self._channel_label()
        max_rounds = blocking_review_max_rounds()
        round_index = 0
        while True:
            # run_blocking_review spawns a counterpart-CPU subprocess that can run
            # for minutes; never block the single event loop on it.
            review = await asyncio.to_thread(
                run_blocking_review,
                resp.audit,
                cpu="codex",
                channel=channel,
                response_content=resp.content,
                round_index=round_index,
            )
            audit = dict(resp.audit or {})
            audit["blocking_review"] = review
            resp.audit = audit
            if review.get("status") != "needs_fix":
                return resp
            if round_index >= max_rounds:
                resp.content = unresolved_review_message(resp.content, review)
                return resp
            resp = await self._run_internal_repair_turn(build_repair_prompt(review))
            round_index += 1

    async def _run_internal_repair_turn(self, prompt: str) -> Response:
        audit = begin_turn(
            cpu="codex",
            channel=self._channel_label(),
            prompt=prompt,
            session_id_before=self._session_id,
            cwd=_codex_cwd(self._memory_source),
            images_count=0,
        )
        try:
            resp = await self._run_codex(prompt, None, None)
        except Exception as e:
            finish_turn(audit, error=e)
            raise
        resp.audit = finish_turn(
            audit,
            response=resp,
            tools=resp.tools,
            tool_uses=resp.audit.get("tool_uses", []) if isinstance(resp.audit, dict) else [],
        )
        return resp

    async def _run_codex(
        self,
        prompt: str,
        images: list[dict[str, str]] | None,
        on_stream: StreamCB | None,
    ) -> Response:
        image_dir = Path(tempfile.mkdtemp(prefix="babata-codex-images."))
        # Only the on-disk path is needed (codex writes the last message via -o);
        # close the fd immediately or the long-lived session leaks one fd/turn
        # and exhausts RLIMIT_NOFILE after a few hundred turns.
        _last_fd, _last_path = tempfile.mkstemp(prefix="babata-codex-last.", text=True)
        os.close(_last_fd)
        last_file = Path(_last_path)
        image_paths: list[Path] = []
        try:
            for img in images or []:
                image_paths.append(_decode_image_to_file(img, image_dir))
            cmd, prompt_stdin, memory_injected = self._build_command(prompt, image_paths, last_file)
            result = await self._run_command(cmd, prompt_stdin, on_stream)
            content = result["content"]
            if last_file.is_file():
                with suppress(Exception):
                    file_content = last_file.read_text().strip()
                    if file_content:
                        content = file_content
            if content and on_stream:
                await on_stream(None, None, content, None)
            sid = result["sid"] or self._session_id or ""
            if sid:
                old_sid = self._session_id
                if sid != old_sid:
                    self._fire_hook(_HOOKS_DIR, "session-start.sh", sid)
                self._session_id = sid
                self._record_codex_turn(sid, prompt, content)
                if memory_injected:
                    self._mark_codex_memory_injected(sid)
                notify_skill_evolve_turn(
                    session_id=sid,
                    cpu="codex",
                    source=self._memory_source,
                    channel=self._channel_label(),
                    state_file=self._state_file,
                    metadata={"tools": result["tools"], "engine": "codex"},
                )
            log_memory_reflex_post_answer(self._memory_reflex_event_id, content)
            self._memory_reflex_event_id = None

            return Response(
                content=content,
                session_id=sid,
                cost=0.0,
                tools=result["tools"],
                audit={"tool_uses": result.get("tool_uses", [])},
                model=os.environ.get("BABATA_CODEX_MODEL") or "codex",
                input_tokens=result["usage"].get("input_tokens", 0),
                output_tokens=result["usage"].get("output_tokens", 0),
                cache_read_tokens=result["usage"].get("cached_input_tokens", 0),
            )
        finally:
            with suppress(Exception):
                last_file.unlink()
            for fp in image_paths:
                with suppress(Exception):
                    fp.unlink()
            with suppress(Exception):
                image_dir.rmdir()

    def _build_command(
        self,
        prompt: str,
        image_paths: list[Path],
        last_file: Path,
    ) -> tuple[list[str], str, bool]:
        memory_context = ""
        source = self._memory_source
        should_inject_memory = self._should_inject_codex_memory()
        if should_inject_memory:
            memory_context, event_id = _render_babata_memory_context_event(
                source,
                prompt,
            )
            self._memory_reflex_event_id = event_id
        else:
            source_name = source or "unknown"
            self._memory_reflex_event_id = log_memory_reflex_preflight_only(
                source=source_name,
                user_prompt=prompt,
                cpu="codex",
                cwd=_codex_cwd(source_name),
            )
        full_prompt = self._build_full_prompt(prompt, memory_context)
        memory_injected = bool(memory_context)
        base = [
            _codex_cli_path(),
            "-c", "notify=[]",
            "-c", 'approval_policy="never"',
            *_codex_mcp_overrides(self._mcp_servers),
        ]
        model = os.environ.get("BABATA_CODEX_MODEL")
        if model:
            base.extend(["-m", model])
        if os.environ.get("BABATA_CODEX_IGNORE_USER_CONFIG") == "1":
            exec_flags = ["--ignore-user-config"]
        else:
            exec_flags = []
        if os.environ.get("BABATA_CODEX_SEARCH") == "1":
            base.append("--search")

        image_args: list[str] = []
        for fp in image_paths:
            image_args.extend(["-i", str(fp)])

        if self._session_id:
            return [
                *base,
                "exec",
                "resume",
                *exec_flags,
                "--json",
                "--skip-git-repo-check",
                "-o", str(last_file),
                *image_args,
                self._session_id,
                "-",
            ], full_prompt, memory_injected
        return [
            *base,
            "exec",
            *exec_flags,
            "--json",
            "--skip-git-repo-check",
            "--sandbox", _codex_sandbox(),
            "-C", _codex_cwd(source),
            "-o", str(last_file),
            *image_args,
            "-",
        ], full_prompt, memory_injected

    def _build_full_prompt(self, prompt: str, memory_context: str) -> str:
        parts = []
        if self._source_prompt:
            parts.append(self._source_prompt)
        if memory_context:
            parts.append(memory_context)
        parts.append(prompt)
        return "\n\n".join(parts)

    def _should_inject_codex_memory(self) -> bool:
        if not _codex_memory_inject_enabled():
            return False
        if os.environ.get("BABATA_CRON_AGENT") == "1":
            return False
        if memory_reflex_mode() == "enforce":
            return True
        if not self._session_id:
            return True
        state = self._load_state()
        injected = state.get(_CODEX_MEMORY_INJECTED_KEY)
        if not isinstance(injected, list):
            return True
        return self._session_id not in {str(sid) for sid in injected}

    def _mark_codex_memory_injected(self, sid: str) -> None:
        state = self._load_state()
        injected = state.get(_CODEX_MEMORY_INJECTED_KEY)
        if not isinstance(injected, list):
            injected = []
        history = [str(item) for item in injected if str(item) != sid]
        history.insert(0, sid)
        state[_CODEX_MEMORY_INJECTED_KEY] = history[:_CODEX_RECENT_LIMIT]
        self._save_state(state)

    async def _run_command(
        self,
        cmd: list[str],
        prompt_stdin: str,
        on_stream: StreamCB | None,
    ) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_CODEX_STREAM_LIMIT,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        proc_stdin = getattr(proc, "stdin", None)
        if proc_stdin is not None:
            with suppress(BrokenPipeError, ConnectionResetError):
                proc_stdin.write(prompt_stdin.encode())
                await proc_stdin.drain()
            with suppress(Exception):
                proc_stdin.close()
            wait_closed = getattr(proc_stdin, "wait_closed", None)
            if wait_closed is not None:
                with suppress(Exception):
                    await wait_closed()
        stderr_task = asyncio.create_task(proc.stderr.read())
        sid: str | None = None
        content = ""
        tools: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        running_tools: dict[str, str] = {}
        usage: dict[str, int] = {}
        failure_message: str | None = None

        def remember_tool(name: str) -> None:
            if name not in tools:
                tools.append(name)

        def remember_tool_use(name: str, item: dict[str, Any]) -> None:
            if not name:
                return
            summary = _codex_tool_summary(name, item)
            if summary not in tool_uses:
                tool_uses.append(summary)

        try:
            stall_timeout = _codex_stall_timeout()
            while True:
                try:
                    if stall_timeout > 0:
                        raw = await asyncio.wait_for(
                            proc.stdout.readline(),
                            timeout=stall_timeout,
                        )
                    else:
                        raw = await proc.stdout.readline()
                except asyncio.TimeoutError:
                    with suppress(ProcessLookupError, Exception):
                        proc.terminate()
                    with suppress(asyncio.TimeoutError, Exception):
                        await asyncio.wait_for(proc.wait(), timeout=5)
                    raise RuntimeError(
                        f"codex stalled: no stdout event for {stall_timeout:.0f}s"
                    )
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("codex non-json stdout: %s", line[:500])
                    continue
                etype = event.get("type")
                if etype == "thread.started":
                    sid = event.get("thread_id") or sid
                    continue
                if etype == "turn.failed":
                    err = event.get("error") or {}
                    if isinstance(err, dict) and err.get("message"):
                        failure_message = str(err.get("message"))
                    else:
                        failure_message = json.dumps(event, ensure_ascii=False)
                    continue
                if etype == "error":
                    failure_message = str(event.get("message") or line)
                    continue
                if etype == "item.started":
                    item = event.get("item") or {}
                    name = _extract_tool_name(item)
                    if name:
                        remember_tool(name)
                        remember_tool_use(name, item)
                        item_id = _extract_tool_id(item)
                        if item_id:
                            running_tools[item_id] = name
                        if on_stream:
                            await on_stream(name, item, None, None)
                    continue
                if etype == "item.completed":
                    item = event.get("item") or {}
                    if item.get("type") == "agent_message":
                        text = str(item.get("text") or "").strip()
                        if text:
                            content = text
                    else:
                        item_id = _extract_tool_id(item)
                        name = _extract_tool_name(item)
                        if not name and item_id:
                            name = running_tools.get(item_id)
                        if name:
                            remember_tool(name)
                            if item_id:
                                running_tools.pop(item_id, None)
                            if on_stream:
                                await on_stream(None, None, None, _extract_tool_result(item))
                    continue
                if etype == "turn.completed":
                    raw_usage = event.get("usage") or {}
                    usage = {
                        "input_tokens": int(raw_usage.get("input_tokens") or 0),
                        "cached_input_tokens": int(raw_usage.get("cached_input_tokens") or 0),
                        "output_tokens": int(raw_usage.get("output_tokens") or 0),
                    }
            rc = await proc.wait()
        except asyncio.CancelledError:
            with suppress(ProcessLookupError, Exception):
                proc.terminate()
            with suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task
            raise
        except Exception as e:
            with suppress(ProcessLookupError, Exception):
                proc.terminate()
            with suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
            stderr_task.cancel()
            with suppress(asyncio.CancelledError):
                await stderr_task
            if _is_codex_split_error(str(e)):
                log.warning("codex stdout split failure: %s", str(e)[:300])
                return {
                    "sid": sid,
                    "content": _split_error_content(content),
                    "tools": tools,
                    "tool_uses": tool_uses,
                    "usage": usage,
                }
            raise

        stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
        error_text = "\n".join(part for part in (failure_message, stderr) if part)
        if rc != 0 or failure_message:
            if _is_codex_split_error(error_text):
                log.warning("codex output split failure: %s", error_text[:300])
                return {
                    "sid": sid,
                    "content": _split_error_content(content),
                    "tools": tools,
                    "tool_uses": tool_uses,
                    "usage": usage,
                }
            raise RuntimeError(error_text or f"codex exited {rc}")
        return {
            "sid": sid,
            "content": content,
            "tools": tools,
            "tool_uses": tool_uses,
            "usage": usage,
        }

    def _record_codex_turn(self, sid: str, prompt: str, content: str) -> None:
        state = self._load_state()
        sessions = state.get(_CODEX_SESSIONS_KEY)
        if not isinstance(sessions, dict):
            sessions = {}
        now = time.time()
        rec = sessions.get(sid) if isinstance(sessions.get(sid), dict) else {}
        first_user = rec.get("first_user") or prompt
        turns = rec.get("turns") if isinstance(rec.get("turns"), list) else []
        turns.append(["user", prompt])
        if content:
            turns.append(["assistant", content])
        rec.update({
            "first_user": first_user,
            "preview": content or prompt,
            "mtime": now,
            "turns": turns[-8:],
        })
        sessions[sid] = rec
        state[_CODEX_SESSIONS_KEY] = sessions
        self._remember_engine_sid(state, sid)
        _record_session_metadata(state, sid, now=now, recent_limit=_CODEX_RECENT_LIMIT)
        self._save_state(state)

    def list_recent_sessions(
        self,
        limit: int = 10,
        channel_filter: list[str] | None = None,
        scan_all_buckets: bool = False,
    ) -> list[dict]:
        del scan_all_buckets
        state = self._load_state()
        sessions = state.get(_CODEX_SESSIONS_KEY) or {}
        own_channel = self._channel_label()
        if channel_filter is not None and own_channel not in channel_filter:
            return []
        out: list[dict] = []
        for sid in state.get("recent_sids", []) or []:
            rec = sessions.get(sid) if isinstance(sessions, dict) else None
            if not isinstance(rec, dict):
                continue
            first_user = str(rec.get("first_user") or "")
            if not first_user:
                continue
            out.append({
                "sid": sid,
                "first_user": first_user,
                "preview": str(rec.get("preview") or first_user),
                "mtime": float(rec.get("mtime") or 0.0),
                "is_current": sid == self._session_id,
                "channel": own_channel,
                "is_own_channel": True,
            })
            if len(out) >= limit:
                break
        return out

    def resume(self, sid: str) -> bool:
        sessions = self._load_state().get(_CODEX_SESSIONS_KEY) or {}
        if sid not in sessions:
            return False
        old_sid = self._session_id
        self._session_id = sid
        self._record_sid(sid)
        if old_sid != sid:
            if old_sid:
                self._fire_hook(_HOOKS_DIR, "session-end.sh", old_sid)
            self._fire_hook(_HOOKS_DIR, "session-start.sh", sid)
        return True

    def get_recent_turns(
        self,
        sid: str,
        pairs: int = 2,
        char_cap: int = 400,
    ) -> list[tuple[str, str]]:
        sessions = self._load_state().get(_CODEX_SESSIONS_KEY) or {}
        rec = sessions.get(sid) if isinstance(sessions, dict) else None
        turns = rec.get("turns") if isinstance(rec, dict) else None
        if not isinstance(turns, list):
            return []
        out: list[tuple[str, str]] = []
        for item in turns[-(2 * pairs):]:
            if not isinstance(item, list) or len(item) != 2:
                continue
            role, text = str(item[0]), str(item[1])
            if len(text) > char_cap:
                text = text[:char_cap].rstrip() + "..."
            out.append((role, text))
        return out

    def is_last_turn_orphan(self, sid: str | None = None) -> bool:
        del sid
        return False

    async def context_usage(self) -> dict[str, Any]:
        raise RuntimeError("/context is not supported by the Codex engine yet")

    def _channel_label(self) -> str:
        from cc import _channel_label_from_state_file
        return _channel_label_from_state_file(self._state_file)


class CodexLiveSession(CodexEngine):
    """LiveSession-shaped wrapper backed by one Codex CLI process per turn."""

    def __init__(
        self,
        *,
        state_file: Path,
        source_prompt: str,
        mcp_servers: dict[str, Any] | None = None,
        memory_source: str | None = None,
    ) -> None:
        super().__init__(
            state_file=state_file,
            source_prompt=source_prompt,
            mcp_servers=mcp_servers,
            memory_source=memory_source,
        )
        self._events: asyncio.Queue[Event | None] = asyncio.Queue()
        self._turn_task: asyncio.Task[None] | None = None
        self._closed = True

    @property
    def is_connected(self) -> bool:
        return not self._closed

    async def connect(self) -> None:
        self._closed = False
        self._check_idle_reset()

    async def close(self) -> None:
        self._closed = True
        await self._cancel_turn()
        await self._events.put(None)
        if self._session_id:
            self._fire_hook(_HOOKS_DIR, "session-end.sh", self._session_id)

    async def reset_live(self) -> Response:
        await self._cancel_turn()
        self.reset()
        return Response(content="会话已重置。", session_id="", cost=0.0)

    async def resume_live(self, sid: str) -> bool:
        await self._cancel_turn()
        return self.resume(sid)

    def submit(self, prompt: str, images: list[dict[str, str]] | None = None) -> None:
        if self._closed:
            raise RuntimeError("CodexLiveSession is not connected")
        if self._turn_task and not self._turn_task.done():
            raise RuntimeError("CodexLiveSession already has an active turn")
        self._turn_task = asyncio.create_task(self._run_live_turn(prompt, images))

    async def interrupt(self) -> None:
        task = self._turn_task
        if not task or task.done():
            return
        await self._cancel_turn()
        await self._events.put(Event(
            kind="turn_end",
            response=Response(
                content="当前 Codex turn 已停止。",
                session_id=self._session_id or "",
                cost=0.0,
                stopped=True,
            ),
        ))

    async def events(self) -> AsyncIterator[Event]:
        while not self._closed:
            ev = await self._events.get()
            if ev is None:
                return
            yield ev

    async def _cancel_turn(self) -> None:
        task = self._turn_task
        self._turn_task = None
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _run_live_turn(
        self,
        prompt: str,
        images: list[dict[str, str]] | None,
    ) -> None:
        try:
            async def _on_stream(tool_name, tool_input, text_chunk, tool_result) -> None:
                if tool_name:
                    await self._events.put(Event(
                        kind="tool_use",
                        name=tool_name,
                        input_dict=tool_input or {},
                    ))
                if tool_result:
                    await self._events.put(Event(
                        kind="tool_result",
                        is_error=bool(tool_result.get("is_error")),
                        text=str(tool_result.get("text") or ""),
                    ))
                if text_chunk:
                    await self._events.put(Event(kind="text_delta", chunk=text_chunk))

            old_sid = self._session_id
            resp = await self.query(prompt, images, _on_stream)
            if resp.session_id and resp.session_id != old_sid:
                await self._events.put(Event(
                    kind="session_changed",
                    old_sid=old_sid,
                    new_sid=resp.session_id,
                ))
            await self._events.put(Event(kind="turn_end", response=resp))
        except Exception as e:
            await self._events.put(Event(kind="error", exception=e))
