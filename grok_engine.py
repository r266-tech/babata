"""Grok CLI engine adapter for babata."""

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
from cli_runtime import env_cli_path
from cc import (
    CC,
    Event,
    Response,
    StreamCB,
    _HOOKS_DIR,
    _channel_label_from_state_file,
    _record_session_metadata,
)
from memory_runtime import (
    default_memory_source,
    log_memory_reflex_post_answer,
    log_memory_reflex_preflight_only,
    memory_inject_enabled,
    memory_inject_timeout,
    memory_reflex_mode,
    render_babata_memory_context_event,
)
from turn_audit import begin_turn, finish_turn, summarize_tool_use

log = logging.getLogger(__name__)

_GROK_SESSIONS_KEY = "grok_sessions"
_GROK_MEMORY_INJECTED_KEY = "grok_memory_injected_sids"
_GROK_RECENT_LIMIT = 200
_GROK_STATE_TEXT_CHARS = 320
_GROK_STATE_TURN_LIMIT = 20
_GROK_STREAM_LIMIT = 16 * 1024 * 1024
_DATA_URL_RE = re.compile(r"^data:([^;,]+)?(?:;[^,]*)?;base64$", re.IGNORECASE)


async def _cancel_task(task: asyncio.Task[Any] | None) -> None:
    if task is None or task.done():
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _terminate_process(proc: asyncio.subprocess.Process, *, timeout: float) -> None:
    if proc.returncode is not None:
        return
    with suppress(ProcessLookupError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
        return
    except asyncio.TimeoutError:
        pass
    with suppress(ProcessLookupError):
        proc.kill()
    with suppress(Exception):
        await proc.wait()


def _grok_cli_path() -> str:
    configured = env_cli_path("BABATA_GROK_CLI_PATH", "GROK_CLI_PATH")
    if configured:
        return configured
    user_install = Path.home() / ".grok" / "bin" / "grok"
    if user_install.is_file():
        return str(user_install)
    return "grok"


def _grok_stall_timeout() -> float:
    raw = os.environ.get("BABATA_GROK_STALL_TIMEOUT", "180")
    try:
        return max(0.0, float(raw))
    except ValueError:
        return 180.0


def _grok_cwd(source: str | None = None) -> str:
    if source == "sidebar":
        return str(Path(__file__).parent)
    if os.environ.get("BABATA_FULL_TRUST") == "1":
        return str(Path.home())
    return str(Path(__file__).parent)


def _grok_permission_mode() -> str:
    configured = os.environ.get("BABATA_GROK_PERMISSION_MODE")
    if configured:
        return configured
    return "bypassPermissions" if os.environ.get("BABATA_FULL_TRUST") == "1" else "dontAsk"


def _grok_model_override() -> str | None:
    raw = os.environ.get("BABATA_GROK_MODEL")
    if raw is None:
        return None
    model = raw.strip()
    if not model or model.lower() in {"auto", "default", "cli-default"}:
        return None
    return model


def _grok_model_label(cli_path: str) -> str:
    return (
        os.environ.get("BABATA_GROK_DISPLAY_MODEL")
        or _grok_model_override()
        or Path(cli_path).name
        or "grok"
    )


def _grok_image_block(img: dict[str, str]) -> dict[str, str]:
    data = str(img.get("data") or "").strip()
    if not data:
        raise ValueError("Grok image input is missing base64 data")
    media_type = str(img.get("media_type") or img.get("mime_type") or "image/png")
    if data.startswith("data:"):
        try:
            header, data = data.split(",", 1)
        except ValueError as exc:
            raise ValueError("Grok image data URL is malformed") from exc
        match = _DATA_URL_RE.match(header)
        if not match:
            raise ValueError("Grok image data URL must be base64 encoded")
        if match.group(1):
            media_type = match.group(1)
    compact = "".join(data.split())
    try:
        base64.b64decode(compact, validate=True)
    except Exception as exc:
        raise ValueError("Grok image input is not valid base64") from exc
    return {
        "type": "image",
        "data": compact,
        "mimeType": media_type,
    }


def _write_grok_prompt_file(prompt: str, images: list[dict[str, str]]) -> Path:
    blocks = [{"type": "text", "text": prompt}]
    blocks.extend(_grok_image_block(img) for img in images)
    fd, raw_path = tempfile.mkstemp(prefix="babata-grok-prompt.", suffix=".json")
    path = Path(raw_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fp:
            json.dump(blocks, fp, ensure_ascii=False)
    except Exception:
        with suppress(Exception):
            os.close(fd)
        with suppress(Exception):
            path.unlink()
        raise
    return path


def _cap_grok_state_text(value: object, cap: int = _GROK_STATE_TEXT_CHARS) -> str:
    text = "" if value is None else str(value)
    if len(text) <= cap:
        return text
    return text[:cap].rstrip() + "..."


def _cap_grok_state_turns(turns: object) -> list[list[str]]:
    if not isinstance(turns, list):
        return []
    out: list[list[str]] = []
    for item in turns[-_GROK_STATE_TURN_LIMIT:]:
        if not isinstance(item, list) or len(item) != 2:
            continue
        role = str(item[0])
        if role not in {"user", "assistant"}:
            continue
        out.append([role, _cap_grok_state_text(item[1])])
    return out


def _render_babata_memory_context_event(
    source: str | None = None,
    user_prompt: str | None = None,
) -> tuple[str, str | None]:
    source_name = source or "unknown"
    return render_babata_memory_context_event(
        enabled=memory_inject_enabled("grok"),
        source=source_name,
        user_prompt=user_prompt,
        cpu="grok",
        cwd=_grok_cwd(source_name),
        timeout=memory_inject_timeout("grok"),
    )


def _tool_result_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return str(value)


def _grok_tool_summary(name: str, event: dict[str, Any]) -> dict[str, Any]:
    tool_input: dict[str, Any] = {}
    for key in ("input", "arguments", "args", "params"):
        value = event.get(key)
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
    if not tool_input and event.get("type"):
        tool_input["type"] = str(event.get("type"))
    return summarize_tool_use(name, tool_input)


class GrokCommandAccumulator:
    def __init__(self, on_stream: StreamCB | None) -> None:
        self.on_stream = on_stream
        self.sid: str | None = None
        self.content = ""
        self.tools: list[str] = []
        self.tool_uses: list[dict[str, Any]] = []
        self.usage: dict[str, int] = {}
        self.failure_message: str | None = None
        self.last_non_json = ""

    def _remember_tool(self, name: str, event: dict[str, Any]) -> None:
        if name not in self.tools:
            self.tools.append(name)
        summary = _grok_tool_summary(name, event)
        if summary not in self.tool_uses:
            self.tool_uses.append(summary)

    async def handle_line(self, line: str) -> None:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            self.last_non_json = line
            return
        if not isinstance(event, dict):
            return

        etype = str(event.get("type") or "")
        if etype == "text":
            chunk = str(event.get("data") or "")
            if chunk:
                self.content += chunk
                if self.on_stream:
                    await self.on_stream(None, None, chunk, None)
            return
        if etype == "end":
            self.sid = str(event.get("sessionId") or event.get("session_id") or self.sid or "")
            stop_reason = str(event.get("stopReason") or event.get("stop_reason") or "")
            if stop_reason and stop_reason not in {"EndTurn", "Stop", "Completed"} and not self.content:
                self.failure_message = f"grok stopped: {stop_reason}"
            return
        if etype in {"error", "turn.failed"}:
            self.failure_message = str(event.get("message") or event.get("error") or line)
            return
        if etype in {"tool_use", "tool.start", "tool_call"}:
            name = str(event.get("name") or event.get("tool") or "")
            if name:
                self._remember_tool(name, event)
                if self.on_stream:
                    await self.on_stream(name, event, None, None)
            return
        if etype in {"tool_result", "tool.end", "tool_output"}:
            name = str(event.get("name") or event.get("tool") or "")
            if name:
                self._remember_tool(name, event)
            if self.on_stream:
                await self.on_stream(None, None, None, {
                    "is_error": bool(event.get("is_error") or event.get("error")),
                    "text": _tool_result_text(
                        event.get("output")
                        if event.get("output") is not None
                        else event.get("result") or event.get("error") or ""
                    ),
                })
            return

        # Non-streaming `--output-format json` shape, accepted for tests and
        # future CLI changes.
        if isinstance(event.get("text"), str):
            self.content = str(event.get("text") or "")
            self.sid = str(event.get("sessionId") or event.get("session_id") or self.sid or "")

    def result(self) -> dict[str, Any]:
        return {
            "sid": self.sid,
            "content": self.content.strip(),
            "tools": self.tools,
            "tool_uses": self.tool_uses,
            "usage": self.usage,
        }


class GrokEngine(CC):
    """One-shot Grok CLI backend with babata-compatible state."""

    supports_hot_input = False

    def __init__(
        self,
        *,
        state_file: Path,
        source_prompt: str,
        mcp_servers: dict[str, Any] | None = None,
        memory_source: str | None = None,
        memory_enabled: bool = True,
    ) -> None:
        super().__init__(
            state_file=state_file,
            source_prompt=source_prompt,
            mcp_servers=mcp_servers,
            memory_source=memory_source or default_memory_source(),
            memory_enabled=memory_enabled,
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
            cpu="grok",
            channel=_channel_label_from_state_file(self._state_file),
            prompt=prompt,
            session_id_before=self._session_id,
            cwd=_grok_cwd(self._memory_source),
            images_count=len(images or []),
        )
        try:
            resp = await self._run_grok(prompt, images, on_stream)
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
        channel = _channel_label_from_state_file(self._state_file)
        max_rounds = blocking_review_max_rounds()
        round_index = 0
        while True:
            review = await asyncio.to_thread(
                run_blocking_review,
                resp.audit,
                cpu="grok",
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
            cpu="grok",
            channel=_channel_label_from_state_file(self._state_file),
            prompt=prompt,
            session_id_before=self._session_id,
            cwd=_grok_cwd(self._memory_source),
            images_count=0,
        )
        try:
            resp = await self._run_grok(prompt, None, None)
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

    async def _run_grok(
        self,
        prompt: str,
        images: list[dict[str, str]] | None,
        on_stream: StreamCB | None,
    ) -> Response:
        cmd, model_label, memory_injected, prompt_file = self._build_command(prompt, images)
        try:
            result = await self._run_command(cmd, on_stream)
        finally:
            if prompt_file:
                with suppress(Exception):
                    prompt_file.unlink()
        content = str(result["content"] or "")
        if content and on_stream:
            # Streaming-json text chunks normally already emitted; this branch is
            # for a future CLI that returns only a final JSON object.
            # Avoid double-emitting the common path.
            pass
        sid = result["sid"] or self._session_id or ""
        if sid:
            old_sid = self._session_id
            if sid != old_sid:
                self._fire_hook(_HOOKS_DIR, "session-start.sh", sid)
            self._session_id = sid
            self._record_grok_turn(sid, prompt, content)
            if memory_injected:
                self._mark_grok_memory_injected(sid)
        log_memory_reflex_post_answer(self._memory_reflex_event_id, content)
        self._memory_reflex_event_id = None
        return Response(
            content=content,
            session_id=sid,
            cost=0.0,
            tools=result["tools"],
            audit={"tool_uses": result.get("tool_uses", [])},
            model=model_label,
        )

    def _build_command(
        self,
        prompt: str,
        images: list[dict[str, str]] | None = None,
    ) -> tuple[list[str], str, bool, Path | None]:
        full_prompt, memory_injected = self._build_prompt(prompt)
        cli = _grok_cli_path()
        cmd = [
            cli,
            "--cwd", _grok_cwd(self._memory_source),
            "--output-format", "streaming-json",
            "--permission-mode", _grok_permission_mode(),
        ]
        if os.environ.get("BABATA_GROK_NATIVE_MEMORY") != "1":
            cmd.append("--no-memory")
        if os.environ.get("BABATA_GROK_DISABLE_WEB_SEARCH") == "1":
            cmd.append("--disable-web-search")
        if os.environ.get("BABATA_GROK_ALWAYS_APPROVE", "1") != "0":
            cmd.append("--always-approve")
        model = _grok_model_override()
        if model:
            cmd.extend(["-m", model])
        effort = os.environ.get("BABATA_GROK_REASONING_EFFORT")
        if effort:
            cmd.extend(["--reasoning-effort", effort])
        if self._session_id:
            cmd.extend(["--resume", self._session_id])
        prompt_file = None
        if images:
            prompt_file = _write_grok_prompt_file(full_prompt, images)
            cmd.extend(["--prompt-file", str(prompt_file)])
        else:
            cmd.extend(["-p", full_prompt])
        return cmd, _grok_model_label(cli), memory_injected, prompt_file

    def _build_prompt(self, prompt: str) -> tuple[str, bool]:
        memory_context = ""
        source = self._memory_source
        if not self._memory_enabled:
            self._memory_reflex_event_id = None
        elif self._should_inject_grok_memory():
            memory_context, event_id = _render_babata_memory_context_event(source, prompt)
            self._memory_reflex_event_id = event_id
        else:
            source_name = source or "unknown"
            self._memory_reflex_event_id = log_memory_reflex_preflight_only(
                source=source_name,
                user_prompt=prompt,
                cpu="grok",
                cwd=_grok_cwd(source_name),
            )
        parts = []
        if self._source_prompt:
            parts.append(self._source_prompt)
        if memory_context:
            parts.append(memory_context)
        parts.append(prompt)
        return "\n\n".join(parts), bool(memory_context)

    def _should_inject_grok_memory(self) -> bool:
        if not memory_inject_enabled("grok"):
            return False
        if memory_reflex_mode() == "enforce":
            return True
        if not self._session_id:
            return True
        injected = self._load_state().get(_GROK_MEMORY_INJECTED_KEY)
        if not isinstance(injected, list):
            return True
        return self._session_id not in {str(sid) for sid in injected}

    def _mark_grok_memory_injected(self, sid: str) -> None:
        state = self._load_state()
        injected = state.get(_GROK_MEMORY_INJECTED_KEY)
        if not isinstance(injected, list):
            injected = []
        history = [str(item) for item in injected if str(item) != sid]
        history.insert(0, sid)
        state[_GROK_MEMORY_INJECTED_KEY] = history[:_GROK_RECENT_LIMIT]
        self._save_state(state)

    async def _run_command(
        self,
        cmd: list[str],
        on_stream: StreamCB | None,
    ) -> dict[str, Any]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_GROK_STREAM_LIMIT,
        )
        assert proc.stdout is not None
        assert proc.stderr is not None
        stderr_task = asyncio.create_task(proc.stderr.read())
        events = GrokCommandAccumulator(on_stream)
        try:
            stall_timeout = _grok_stall_timeout()
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
                    await _terminate_process(proc, timeout=5)
                    stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
                    detail = f"\n{stderr[-2000:]}" if stderr else ""
                    raise RuntimeError(
                        f"grok stalled: no stdout event for {stall_timeout:.0f}s{detail}"
                    )
                if not raw:
                    break
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                await events.handle_line(line)
            rc = await proc.wait()
        except asyncio.CancelledError:
            await _terminate_process(proc, timeout=2)
            await _cancel_task(stderr_task)
            raise
        except Exception:
            await _terminate_process(proc, timeout=2)
            await _cancel_task(stderr_task)
            raise

        stderr = (await stderr_task).decode("utf-8", errors="replace").strip()
        error_text = "\n".join(part for part in (events.failure_message, stderr) if part)
        if rc != 0 or events.failure_message:
            raise RuntimeError(error_text or f"grok exited {rc}")
        return events.result()

    def _record_grok_turn(self, sid: str, prompt: str, content: str) -> None:
        state = self._load_state()
        sessions = state.get(_GROK_SESSIONS_KEY)
        if not isinstance(sessions, dict):
            sessions = {}
        now = time.time()
        rec = sessions.get(sid) if isinstance(sessions.get(sid), dict) else {}
        user_text = _cap_grok_state_text(prompt)
        assistant_text = _cap_grok_state_text(content)
        first_user = _cap_grok_state_text(rec.get("first_user") or user_text)
        turns = _cap_grok_state_turns(rec.get("turns"))
        turns.append(["user", user_text])
        if content:
            turns.append(["assistant", assistant_text])
        rec.update({
            "first_user": first_user,
            "mtime": now,
            "turns": turns[-_GROK_STATE_TURN_LIMIT:],
        })
        rec.pop("preview", None)
        sessions[sid] = rec
        state[_GROK_SESSIONS_KEY] = sessions
        self._remember_engine_sid(state, sid)
        _record_session_metadata(state, sid, now=now, recent_limit=_GROK_RECENT_LIMIT)
        self._save_state(state)

    def list_recent_sessions(
        self,
        limit: int = 10,
        channel_filter: list[str] | None = None,
        scan_all_buckets: bool = False,
    ) -> list[dict]:
        del scan_all_buckets
        state = self._load_state()
        sessions = state.get(_GROK_SESSIONS_KEY) or {}
        own_channel = _channel_label_from_state_file(self._state_file)
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
            turns = _cap_grok_state_turns(rec.get("turns"))
            preview = turns[-1][1] if turns else str(rec.get("preview") or first_user)
            out.append({
                "sid": sid,
                "first_user": first_user,
                "preview": preview,
                "mtime": float(rec.get("mtime") or 0.0),
                "is_current": sid == self._session_id,
                "channel": own_channel,
                "is_own_channel": True,
            })
            if len(out) >= limit:
                break
        return out

    def resume(self, sid: str) -> bool:
        sessions = self._load_state().get(_GROK_SESSIONS_KEY) or {}
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
        sessions = self._load_state().get(_GROK_SESSIONS_KEY) or {}
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
        raise RuntimeError("/context is not supported by the Grok engine yet")


class GrokLiveSession(GrokEngine):
    """LiveSession-shaped wrapper backed by one Grok CLI process per turn."""

    def __init__(
        self,
        *,
        state_file: Path,
        source_prompt: str,
        mcp_servers: dict[str, Any] | None = None,
        memory_source: str | None = None,
        memory_enabled: bool = True,
    ) -> None:
        super().__init__(
            state_file=state_file,
            source_prompt=source_prompt,
            mcp_servers=mcp_servers,
            memory_source=memory_source,
            memory_enabled=memory_enabled,
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
            raise RuntimeError("GrokLiveSession is not connected")
        if self._turn_task and not self._turn_task.done():
            raise RuntimeError("GrokLiveSession already has an active turn")
        self._turn_task = asyncio.create_task(self._run_live_turn(prompt, images))

    async def interrupt(self) -> None:
        task = self._turn_task
        if not task or task.done():
            return
        await self._cancel_turn()
        await self._events.put(Event(
            kind="turn_end",
            response=Response(
                content="当前 Grok turn 已停止。",
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
