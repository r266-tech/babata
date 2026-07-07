import asyncio
import base64
import hashlib
import inspect
import json
import os
import time
import uuid
from pathlib import Path

import cc as cc_module
import sidebar_bot
import sidebar_events
from sidebar_tool_registry import SIDEBAR_TOOLS, tool_specs


def test_cc_exposes_public_session_helpers(tmp_path):
    state_file = tmp_path / "session.json"
    state_file.write_text('{"session_id": "sid-existing", "recent_sids": ["sid-existing", 42, ""]}')

    session = cc_module.CC(state_file=state_file, source_prompt="Source: test.")

    assert session.session_id == "sid-existing"
    assert session.assistant_engine_name is None
    assert session.recent_session_ids() == ["sid-existing"]
    session.persist_current_session()
    state = json.loads(state_file.read_text())
    assert state["session_id"] == "sid-existing"
    assert state["recent_sids"] == ["sid-existing"]
    assert "last_activity_at" in state


def test_sidebar_evidence_bound_helpers_opt_out_of_memory():
    assert sidebar_bot.agent_view_cc._memory_enabled is False
    assert sidebar_bot.clean_read_cc._memory_enabled is False
    assert sidebar_bot.cc._memory_enabled is True
    assert sidebar_bot.proactive_cc._memory_enabled is True


def test_cc_memory_opt_out_skips_context_render(monkeypatch, tmp_path):
    def fail_render(*_args, **_kwargs):
        raise AssertionError("memory context should not render")

    monkeypatch.setattr(cc_module, "_render_babata_memory_context_event", fail_render)
    session = cc_module.CC(
        state_file=tmp_path / "session.json",
        source_prompt="Source: evidence-bound.",
        memory_source="sidebar",
        memory_enabled=False,
    )

    assert session._source_prompt_with_memory(user_prompt="hello") == "Source: evidence-bound."


def test_recent_session_files_scans_buckets_and_excludes_summary_sandbox(monkeypatch, tmp_path):
    projects_root = tmp_path / "projects"
    own_bucket = projects_root / "own-cwd"
    other_bucket = projects_root / "other-cwd"
    summary_cwd = tmp_path / "summary-sandbox"
    summary_bucket = projects_root / str(summary_cwd.resolve()).replace("/", "-")
    for bucket in (own_bucket, other_bucket, summary_bucket):
        bucket.mkdir(parents=True)

    own_file = own_bucket / "own.jsonl"
    other_file = other_bucket / "other.jsonl"
    summary_file = summary_bucket / "summary.jsonl"
    for fp, mtime in ((own_file, 10), (other_file, 30), (summary_file, 20)):
        fp.write_text("{}\n")
        os.utime(fp, (mtime, mtime))

    monkeypatch.setattr(cc_module, "_CC_PROJECTS", own_bucket)
    monkeypatch.setattr(cc_module, "_SUMMARY_SANDBOX", summary_cwd)

    assert cc_module._recent_session_files(scan_all_buckets=False) == [own_file]
    assert cc_module._recent_session_files(scan_all_buckets=True) == [other_file, own_file]
    source = inspect.getsource(cc_module._spawn_summary_generation)
    assert "20字内概括会话主题，只输出一句中文。" in source
    assert "不加任何前缀" not in source


def test_first_real_user_and_entrypoint_skips_synthetic_user_records(tmp_path):
    session_file = tmp_path / "sid.jsonl"
    session_file.write_text(
        "\n".join([
            json.dumps({
                "type": "user",
                "entrypoint": "cli",
                "message": {"content": "<command-name>/status</command-name>"},
            }),
            json.dumps({
                "type": "assistant",
                "message": {"content": "ignored"},
            }),
            json.dumps({
                "type": "user",
                "entrypoint": "sdk-cli",
                "message": {"content": [{"type": "text", "text": "真实问题"}]},
            }),
        ])
        + "\n"
    )

    assert cc_module._first_real_user_and_entrypoint(session_file) == ("真实问题", "sdk-cli")


def test_complete_jsonl_prefix_trims_racing_tail_records():
    assert cc_module._complete_jsonl_prefix(b'{"ok": 1}\n{"partial":') == b'{"ok": 1}\n'
    assert cc_module._complete_jsonl_prefix(b'{"partial":') == b""
    assert cc_module._complete_jsonl_prefix(b"not-json\n") == b""
    assert cc_module._complete_jsonl_prefix(b'{"ok": 1}\nnot-json\n') == b'{"ok": 1}\n'
    assert cc_module._complete_jsonl_prefix(b'{"ok": 1}\n') == b'{"ok": 1}\n'
    assert cc_module._complete_jsonl_prefix(b"") == b""


def test_import_jsonl_to_bucket_forks_and_trims_racing_tail(monkeypatch, tmp_path):
    projects_root = tmp_path / "projects"
    own_bucket = projects_root / "own-cwd"
    other_bucket = projects_root / "other-cwd"
    own_bucket.mkdir(parents=True)
    other_bucket.mkdir(parents=True)
    source = other_bucket / "source-sid.jsonl"
    source_content = b'{"type": "user"}\n{"partial":'
    source.write_bytes(source_content)

    monkeypatch.setattr(cc_module, "_CC_PROJECTS", own_bucket)
    monkeypatch.setattr(uuid, "uuid4", lambda: "new-sid")

    imported_sid = cc_module._import_jsonl_to_bucket("source-sid")

    assert imported_sid == "new-sid"
    assert source.read_bytes() == source_content
    assert (own_bucket / "new-sid.jsonl").read_bytes() == b'{"type": "user"}\n'
    assert not (own_bucket / ".new-sid.jsonl.tmp").exists()
    assert not (own_bucket / ".import-source-sid.lock").exists()


def _write_session_jsonl(path: Path, text: str, *, entrypoint: str = "cli", mtime: int = 10):
    path.write_text(
        json.dumps({
            "type": "user",
            "entrypoint": entrypoint,
            "message": {"content": text},
        }) + "\n"
    )
    os.utime(path, (mtime, mtime))


def test_list_recent_sessions_filters_before_summary_generation(monkeypatch, tmp_path):
    owned = tmp_path / "owned.jsonl"
    oneshot = tmp_path / "oneshot.jsonl"
    term = tmp_path / "term.jsonl"
    _write_session_jsonl(owned, "owned prompt", entrypoint="cli", mtime=30)
    _write_session_jsonl(oneshot, "oneshot prompt", entrypoint="sdk-cli", mtime=20)
    _write_session_jsonl(term, "term prompt", entrypoint="cli", mtime=10)
    spawned: list[tuple[str, float]] = []

    monkeypatch.setattr(cc_module, "_recent_session_files", lambda **_kw: [owned, oneshot, term])
    monkeypatch.setattr(cc_module, "_scan_peer_sids", lambda: {"owned": ["巴巴塔"]})
    monkeypatch.setattr(cc_module, "_load_summary_cache", lambda: {})
    monkeypatch.setattr(cc_module, "_spawn_summary_generation", lambda sid, mtime: spawned.append((sid, mtime)))
    monkeypatch.setattr(cc_module, "_channel_label_from_state_file", lambda _fp: "巴巴塔")

    session = cc_module.CC(state_file=tmp_path / "babata-session.json", source_prompt="Source: test.")
    rows = session.list_recent_sessions(limit=10, channel_filter=["term"])

    assert [row["sid"] for row in rows] == ["term"]
    assert rows[0]["preview"] == "term prompt"
    assert spawned == [("term", term.stat().st_mtime)]


def test_list_recent_sessions_uses_cached_preview_and_owner_flags(monkeypatch, tmp_path):
    owned = tmp_path / "owned.jsonl"
    oneshot = tmp_path / "oneshot.jsonl"
    _write_session_jsonl(owned, "owned prompt", entrypoint="cli", mtime=30)
    _write_session_jsonl(oneshot, "oneshot prompt", entrypoint="sdk-cli", mtime=20)
    spawned: list[str] = []

    monkeypatch.setattr(cc_module, "_recent_session_files", lambda **_kw: [owned, oneshot])
    monkeypatch.setattr(cc_module, "_scan_peer_sids", lambda: {"owned": ["巴巴塔2", "巴巴塔"]})
    monkeypatch.setattr(cc_module, "_load_summary_cache", lambda: {
        "owned": {"summary": "cached summary", "source_mtime": owned.stat().st_mtime}
    })
    monkeypatch.setattr(cc_module, "_spawn_summary_generation", lambda sid, _mtime: spawned.append(sid))
    monkeypatch.setattr(cc_module, "_channel_label_from_state_file", lambda _fp: "巴巴塔")

    state_file = tmp_path / "babata-session.json"
    state_file.write_text(json.dumps({"session_id": "owned"}))
    session = cc_module.CC(state_file=state_file, source_prompt="Source: test.")
    rows = session.list_recent_sessions(limit=10)

    assert rows[0] == {
        "sid": "owned",
        "first_user": "owned prompt",
        "preview": "cached summary",
        "mtime": owned.stat().st_mtime,
        "is_current": True,
        "channel": "巴巴塔2",
        "is_own_channel": True,
    }
    assert rows[1]["channel"] == "oneshot"
    assert rows[1]["preview"] == "oneshot prompt"
    assert spawned == ["oneshot"]


def test_sidebar_cpu_status_reads_public_session_property(monkeypatch):
    class FakeLock:
        def __init__(self, locked: bool):
            self._locked = locked

        def locked(self):
            return self._locked

    class FakeEngine:
        def __init__(self, name: str = "codex", sid: str | None = None):
            self._name = name
            self._sid = sid

        @property
        def session_id(self):
            return self._sid

        @property
        def assistant_engine_name(self):
            return self._name

    def engine_name_for(obj, _state_file: Path) -> str:
        return obj._name

    monkeypatch.setattr(sidebar_bot, "_engine_name_for", engine_name_for)
    monkeypatch.setattr(sidebar_bot, "_cc_lock", FakeLock(False))
    monkeypatch.setattr(sidebar_bot, "_proactive_lock", FakeLock(True))
    monkeypatch.setattr(sidebar_bot, "cc", FakeEngine("codex", "sid-visible"))
    monkeypatch.setattr(sidebar_bot, "proactive_cc", FakeEngine("codex"))

    payload = sidebar_bot._cpu_status_payload()

    assert payload["session_id"] == "sid-visible"
    assert payload["busy"] is False
    assert payload["chat_busy"] is False
    assert payload["proactive_busy"] is True


def test_sidebar_cpu_switch_persists_sessions_with_public_api(monkeypatch, tmp_path):
    class FakeLock:
        def __init__(self, locked: bool):
            self._locked = locked

        def locked(self):
            return self._locked

    class FakeEngine:
        def __init__(self, name: str = "codex"):
            self._name = name
            self.persisted = 0

        @property
        def session_id(self):
            return None

        @property
        def assistant_engine_name(self):
            return self._name

        def persist_current_session(self):
            self.persisted += 1

    def engine_name_for(obj, _state_file: Path) -> str:
        return obj._name

    current_chat = FakeEngine()
    current_proactive = FakeEngine()
    made: list[FakeEngine] = []

    def make_engine(target: str) -> FakeEngine:
        engine = FakeEngine(target)
        made.append(engine)
        return engine

    monkeypatch.setattr(sidebar_bot, "_engine_name_for", engine_name_for)
    monkeypatch.setattr(sidebar_bot, "_cc_lock", FakeLock(False))
    monkeypatch.setattr(sidebar_bot, "_proactive_lock", FakeLock(False))
    monkeypatch.setattr(sidebar_bot, "cc", current_chat)
    monkeypatch.setattr(sidebar_bot, "proactive_cc", current_proactive)
    monkeypatch.setattr(sidebar_bot, "_SIDEBAR_SESSION_FILE", tmp_path / "sidebar.json")
    monkeypatch.setattr(sidebar_bot, "_PROACTIVE_SESSION_FILE", tmp_path / "proactive.json")
    monkeypatch.setattr(sidebar_bot, "_make_sidebar_engine", make_engine)
    monkeypatch.setattr(sidebar_bot, "_make_proactive_engine", make_engine)

    payload = asyncio.run(sidebar_bot._switch_sidebar_cpu("claude"))

    assert payload["changed"] is True
    assert payload["cpu"] == "claude"
    assert current_chat.persisted == 1
    assert current_proactive.persisted == 1
    assert [engine.persisted for engine in made] == [1, 1]


def test_sidebar_cpu_switch_only_waits_for_chat_turn(monkeypatch, tmp_path):
    class FakeLock:
        def __init__(self, locked: bool):
            self._locked = locked

        def locked(self):
            return self._locked

    class FakeEngine:
        def __init__(self, name: str = "claude"):
            self._name = name

        @property
        def session_id(self):
            return None

        @property
        def assistant_engine_name(self):
            return self._name

        def persist_current_session(self):
            return None

    def engine_name_for(obj, _state_file: Path) -> str:
        return obj._name

    monkeypatch.setattr(sidebar_bot, "_engine_name_for", engine_name_for)
    monkeypatch.setattr(sidebar_bot, "cc", FakeEngine())
    monkeypatch.setattr(sidebar_bot, "proactive_cc", FakeEngine())
    monkeypatch.setattr(sidebar_bot, "_SIDEBAR_SESSION_FILE", tmp_path / "sidebar.json")
    monkeypatch.setattr(sidebar_bot, "_PROACTIVE_SESSION_FILE", tmp_path / "proactive.json")
    monkeypatch.setattr(sidebar_bot, "_make_sidebar_engine", lambda target: FakeEngine(target))
    monkeypatch.setattr(sidebar_bot, "_make_proactive_engine", lambda target: FakeEngine(target))

    monkeypatch.setattr(sidebar_bot, "_cc_lock", FakeLock(False))
    monkeypatch.setattr(sidebar_bot, "_proactive_lock", FakeLock(True))
    payload = asyncio.run(sidebar_bot._switch_sidebar_cpu("codex"))
    assert payload["changed"] is True

    monkeypatch.setattr(sidebar_bot, "cc", FakeEngine("claude"))
    monkeypatch.setattr(sidebar_bot, "proactive_cc", FakeEngine("claude"))
    monkeypatch.setattr(sidebar_bot, "_cc_lock", FakeLock(True))
    monkeypatch.setattr(sidebar_bot, "_proactive_lock", FakeLock(False))
    try:
        asyncio.run(sidebar_bot._switch_sidebar_cpu("codex"))
    except RuntimeError as exc:
        assert "sidebar turn" in str(exc)
    else:
        raise AssertionError("CPU switch should wait for active chat turns")


def test_sidebar_cpu_switch_does_not_reach_into_engine_private_session_state():
    source = Path(sidebar_bot.__file__).read_text()

    assert 'getattr(cc, "_session_id"' not in source
    assert 'getattr(obj, "_babata_engine_name"' not in source
    assert "._record_sid(" not in source


def test_sidebar_engine_name_falls_back_without_engine_accessor(tmp_path):
    class NamelessEngine:
        pass

    state_file = tmp_path / "sidebar.json"
    state_file.write_text('{"assistant_engine": "codex"}')

    assert sidebar_bot._engine_name_for(NamelessEngine(), state_file) == "codex"


def test_sidebar_chat_input_builds_prompt_boundary(monkeypatch, tmp_path):
    remembered: list[dict] = []
    cleanup_path = tmp_path / "video.mp4"

    async def process_attachments(raw):
        assert raw == ["attachment"]
        return (
            [{"media_type": "image/png", "data": "abc"}],
            ["[image attached: a.png]"],
            [cleanup_path],
        )

    monkeypatch.setattr(sidebar_bot, "_remember_page_context", remembered.append)
    monkeypatch.setattr(sidebar_bot, "_format_page_context", lambda _ctx: "[page ctx]")
    monkeypatch.setattr(
        sidebar_bot,
        "_format_page_memory",
        lambda _ctx: (_ for _ in ()).throw(AssertionError("page memory should stay opt-in")),
    )
    monkeypatch.setattr(sidebar_bot, "_page_context_bound_meta", lambda _ctx: ("https://x.test", "X"))
    monkeypatch.setattr(sidebar_bot, "_process_attachments", process_attachments)

    chat_input = asyncio.run(sidebar_bot._build_sidebar_chat_input(
        {
            "page_context": {"url": "https://x.test", "title": "X"},
            "attachments": ["attachment"],
        },
        "hello",
    ))

    assert remembered == [{"url": "https://x.test", "title": "X"}]
    assert chat_input.prompt == "[page ctx]\n\n[image attached: a.png]\n\nhello"
    assert chat_input.images == [{"media_type": "image/png", "data": "abc"}]
    assert chat_input.cleanup_paths == [cleanup_path]
    assert chat_input.chat_url == "https://x.test"
    assert chat_input.chat_title == "X"
    assert chat_input.has_attach is True


def test_sidebar_chat_input_includes_page_memory_for_continuation(monkeypatch):
    monkeypatch.setattr(sidebar_bot, "_remember_page_context", lambda _ctx: None)
    monkeypatch.setattr(sidebar_bot, "_format_page_context", lambda _ctx: "[page ctx]")
    monkeypatch.setattr(sidebar_bot, "_format_page_memory", lambda _ctx: "[page memory]")
    monkeypatch.setattr(sidebar_bot, "_page_context_bound_meta", lambda _ctx: ("https://x.test", "X"))
    monkeypatch.setattr(sidebar_bot, "_process_attachments", lambda _raw: asyncio.sleep(0, result=([], [], [])))

    chat_input = asyncio.run(sidebar_bot._build_sidebar_chat_input(
        {"page_context": {"url": "https://x.test", "title": "X"}},
        "继续刚才这个页面",
    ))

    assert chat_input.prompt == "[page ctx]\n\n[page memory]\n\n继续刚才这个页面"


def test_sidebar_user_turn_records_event_history_and_boundary(monkeypatch):
    events: list[tuple] = []
    history: list[tuple] = []
    boundaries: list[str] = []
    monkeypatch.setattr(
        sidebar_bot.sidebar_events,
        "append",
        lambda *args, **kwargs: events.append((args, kwargs)),
    )
    monkeypatch.setattr(
        sidebar_bot.sidebar_history,
        "append",
        lambda *args, **kwargs: history.append((args, kwargs)),
    )
    monkeypatch.setattr(
        sidebar_bot.sidebar_history,
        "boundary",
        lambda: boundaries.append("boundary"),
    )
    chat_input = sidebar_bot.SidebarChatInput(
        prompt="hello",
        images=[{"media_type": "image/png", "data": "abc"}],
        cleanup_paths=[],
        chat_url="https://x.test",
        chat_title="X",
        has_attach=True,
    )

    sidebar_bot._record_sidebar_user_turn("hello", chat_input)
    sidebar_bot._record_sidebar_user_turn("/new", chat_input)

    assert events == [
        (("https://x.test", "chat_turn"), {
            "message_sha256": hashlib.sha256(b"hello").hexdigest(),
            "message_bytes": 5,
        }),
    ]
    assert history == [
        (("user", "hello"), {
            "url": "https://x.test",
            "title": "X",
            "has_image": True,
            "has_attach": True,
        }),
    ]
    assert boundaries == ["boundary"]


def test_sidebar_page_memory_omits_user_message_preview():
    now = int(time.time())
    line = sidebar_events._format_page_memory(
        [
            {
                "ts": now,
                "kind": "chat_turn",
                "message_sha256": hashlib.sha256("secret question".encode()).hexdigest(),
                "message_bytes": len("secret question".encode()),
            },
        ],
        now,
    )

    assert "1 chat turns" in line
    assert "15 bytes" not in line
    assert "secret question" not in line
    assert "last said" not in line


def test_sidebar_assistant_turn_records_only_completed_response(monkeypatch):
    history: list[tuple] = []
    monkeypatch.setattr(
        sidebar_bot.sidebar_history,
        "append",
        lambda *args, **kwargs: history.append((args, kwargs)),
    )
    chat_input = sidebar_bot.SidebarChatInput(
        prompt="hello",
        images=[],
        cleanup_paths=[],
        chat_url="https://x.test",
        chat_title="X",
        has_attach=False,
    )
    trace = sidebar_bot.SidebarStreamTrace(object())
    trace.assistant_text_parts.append("answer")
    trace.tool_trace.append({"name": "dom_read", "status": "done"})

    sidebar_bot._record_sidebar_assistant_turn("hello", chat_input, trace, done_ok=True)
    sidebar_bot._record_sidebar_assistant_turn("hello", chat_input, trace, done_ok=False)
    sidebar_bot._record_sidebar_assistant_turn("/new", chat_input, trace, done_ok=True)

    assert history == [
        (("assistant", "answer"), {
            "url": "https://x.test",
            "tool_trace": [{"name": "dom_read", "status": "done"}],
        }),
    ]


def test_sidebar_sse_headers_include_no_buffer_and_cors(monkeypatch):
    monkeypatch.setattr(
        sidebar_bot,
        "_cors_headers",
        lambda _request: {"access-control-allow-origin": "https://x.test"},
    )

    headers = sidebar_bot._sidebar_sse_headers(object())

    assert headers == {
        "content-type": "text/event-stream; charset=utf-8",
        "cache-control": "no-cache, no-transform",
        "x-accel-buffering": "no",
        "connection": "keep-alive",
        "access-control-allow-origin": "https://x.test",
    }


def test_sidebar_rejects_arbitrary_extension_origin_by_default(monkeypatch):
    monkeypatch.delenv("BABATA_SIDEBAR_ALLOW_ANY_EXTENSION_ORIGIN", raising=False)

    assert sidebar_bot._origin_allowed(f"chrome-extension://{sidebar_bot._DEFAULT_EXTENSION_ID}")
    assert not sidebar_bot._origin_allowed("chrome-extension://not-the-babata-extension")


def test_sidebar_allows_arbitrary_extension_origin_only_when_opted_in(monkeypatch):
    monkeypatch.setenv("BABATA_SIDEBAR_ALLOW_ANY_EXTENSION_ORIGIN", "1")

    assert sidebar_bot._origin_allowed("chrome-extension://not-the-babata-extension")


def test_sidebar_process_attachments_routes_images_files_and_video(monkeypatch, tmp_path):
    async def fake_understand_video(path):
        assert path.read_bytes() == b"video bytes"
        return "video summary"

    def fake_inbound_path(suffix: str) -> Path:
        return tmp_path / f"inbound{suffix}"

    monkeypatch.setattr(sidebar_bot, "understand_video", fake_understand_video)
    monkeypatch.setattr(sidebar_bot, "_inbound_path", fake_inbound_path)

    raw = [
        {
            "kind": "image",
            "name": "shot.png",
            "mime": "image/png",
            "data_base64": base64.b64encode(b"image bytes").decode(),
        },
        {
            "kind": "file",
            "name": "notes",
            "mime": "text/plain",
            "data_base64": base64.b64encode(b"note body").decode(),
        },
        {
            "kind": "video",
            "name": "clip.mov",
            "mime": "video/quicktime",
            "data_base64": base64.b64encode(b"video bytes").decode(),
        },
    ]

    images, lines, cleanup = asyncio.run(sidebar_bot._process_attachments(raw))

    assert images == [{
        "media_type": "image/png",
        "data": raw[0]["data_base64"],
    }]
    assert lines == [
        "[image attached: shot.png]",
        f"[file: {tmp_path / 'inbound-notes.txt'}]",
        "[video clip.mov] video summary",
    ]
    assert (tmp_path / "inbound-notes.txt").read_bytes() == b"note body"
    assert cleanup == [tmp_path / "inbound.mp4"]


def test_sidebar_stream_trace_emits_sse_and_closes_running_tool(monkeypatch):
    events: list[dict] = []

    async def fake_sse_write(_resp, payload):
        events.append(payload)

    async def run():
        monkeypatch.setattr(sidebar_bot, "_sse_write", fake_sse_write)
        trace = sidebar_bot.SidebarStreamTrace(object())

        await trace.on_stream(None, None, "hello", None)
        await trace.on_stream("dom_click", {"selector": "#go"}, None, None)
        await trace.close_running_tools()

        assert trace.assistant_text() == "hello"
        assert len(trace.tool_trace) == 1
        assert trace.tool_trace[0]["name"] == "dom_click"
        assert trace.tool_trace[0]["status"] == "done"
        assert trace.tool_trace[0]["is_error"] is False
        assert trace.tool_trace[0]["result"] == ""

    asyncio.run(run())

    assert events == [
        {"type": "text_delta", "text": "hello"},
        {
            "type": "tool_use",
            "trace_id": "tool-1",
            "name": "dom_click",
            "input": {"selector": "#go"},
        },
        {
            "type": "tool_result",
            "trace_id": "tool-1",
            "is_error": False,
            "text": "",
        },
    ]


def test_sidebar_stream_trace_records_explicit_tool_result(monkeypatch):
    events: list[dict] = []

    async def fake_sse_write(_resp, payload):
        events.append(payload)

    async def run():
        monkeypatch.setattr(sidebar_bot, "_sse_write", fake_sse_write)
        trace = sidebar_bot.SidebarStreamTrace(object())

        await trace.on_stream("dom_read", {"selector": "main"}, None, None)
        await trace.on_stream(None, None, None, {"text": "missing", "is_error": True})

        assert len(trace.tool_trace) == 1
        assert trace.tool_trace[0]["status"] == "error"
        assert trace.tool_trace[0]["is_error"] is True
        assert trace.tool_trace[0]["result"] == "missing"

    asyncio.run(run())

    assert events[-1] == {
        "type": "tool_result",
        "trace_id": "tool-1",
        "is_error": True,
        "text": "missing",
    }


def test_sidebar_source_prompt_does_not_duplicate_tool_inventory():
    prompt = sidebar_bot._SIDEBAR_SOURCE_PROMPT

    assert "MCP schema 为准" in prompt
    assert len(prompt) <= 260
    for tool in SIDEBAR_TOOLS:
        assert tool["name"] not in prompt
        assert "prompt" not in tool


def test_proactive_sidebar_mcp_scope_stays_read_only_and_suggestive():
    names = {tool["name"] for tool in tool_specs("proactive")}

    assert names == {"tab_metadata", "page_snapshot", "suggest_prompts", "mascot_speak"}
    assert not {
        "dom_inject",
        "dom_set",
        "dom_click",
        "page_click_ref",
        "tab_navigate",
        "tabs_close",
        "bookmarks_create",
    } & names
    assert sidebar_bot.proactive_cc._mcp_servers["sidebar"]["env"]["BABATA_SIDEBAR_MCP_SCOPE"] == "proactive"
    assert "BABATA_SIDEBAR_MCP_SCOPE" not in sidebar_bot.cc._mcp_servers["sidebar"].get("env", {})


def test_sidebar_model_visible_tool_descriptions_stay_compact():
    descriptions = [tool["description"] for tool in SIDEBAR_TOOLS]
    by_name = {tool["name"]: tool["description"] for tool in SIDEBAR_TOOLS}
    schema_descriptions = [
        prop["description"]
        for tool in SIDEBAR_TOOLS
        for prop in tool["inputSchema"].get("properties", {}).values()
        if isinstance(prop, dict) and prop.get("description")
    ]

    assert sum(len(description) for description in descriptions) <= 1250
    assert max(len(description) for description in descriptions) <= 100
    assert schema_descriptions == []
    target_fields = [
        prop
        for tool in SIDEBAR_TOOLS
        for name, prop in tool["inputSchema"].get("properties", {}).items()
        if name in ("tab_id", "window_id")
    ]
    assert target_fields
    assert all("description" not in prop for prop in target_fields)
    for marker in (
        "共同进化",
        "哲学",
        "身份认同",
        "prompt chips",
        "高杠杆",
        "V's",
        "V-requested",
    ):
        assert all(marker not in description for description in descriptions)
        assert all(marker not in description for description in schema_descriptions)
    assert "Not trusted input" in by_name["dom_click"]
    assert "translation uses /translate" in by_name["dom_inject"]
    assert "translation uses /translate" in by_name["dom_set"]
    assert "don't shell/curl current tab" in by_name["article_extract"]
    assert "[] clears" in by_name["suggest_prompts"]
    tabs_group = next(tool for tool in SIDEBAR_TOOLS if tool["name"] == "tabs_group")
    assert tabs_group["inputSchema"]["properties"]["color"]["enum"] == [
        "grey",
        "blue",
        "red",
        "yellow",
        "green",
        "pink",
        "purple",
        "cyan",
        "orange",
    ]
