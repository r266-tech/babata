import ast
import asyncio
import inspect
import json
import os
import sys
from pathlib import Path
import pytest

_REPO = Path(__file__).resolve().parents[1]
_SDK_SITE = next(iter((_REPO / ".venv/lib").glob("python*/site-packages")), None)
if _SDK_SITE:
    sys.path.insert(0, str(_SDK_SITE))
sys.path.insert(0, str(_REPO))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("ALLOWED_USER_ID", "0")

import bot
import bridge as tg_bridge
import tg_mcp


def test_tg_voice_clone_pending_consumes_valid_state(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "STATE_DIR", tmp_path)
    monkeypatch.setattr(bot, "INSTANCE", "test")
    monkeypatch.setattr(bot.time, "time", lambda: 10)
    state_file = tmp_path / "voice-clone-pending-test.json"
    state_file.write_text(json.dumps({"expires_at": 20}), encoding="utf-8")

    keep_wav = bot._consume_voice_clone_pending("voice-file")

    assert keep_wav == Path("/tmp/voice-clone-test-voice-file.wav")
    assert not state_file.exists()
    assert not state_file.with_suffix(".json.consumed").exists()


def test_tg_voice_clone_pending_drops_stale_state(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "STATE_DIR", tmp_path)
    monkeypatch.setattr(bot, "INSTANCE", "test")
    monkeypatch.setattr(bot.time, "time", lambda: 10)
    state_file = tmp_path / "voice-clone-pending-test.json"
    state_file.write_text(json.dumps({"expires_at": 9}), encoding="utf-8")

    keep_wav = bot._consume_voice_clone_pending("voice-file")

    assert keep_wav is None
    assert not state_file.exists()


def test_tg_voice_clone_pending_ignores_missing_or_malformed_state(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(bot, "STATE_DIR", tmp_path)
    monkeypatch.setattr(bot, "INSTANCE", "test")
    monkeypatch.setattr(bot.time, "time", lambda: 10)
    assert bot._consume_voice_clone_pending("voice-file") is None

    malformed = tmp_path / "voice-clone-pending-test.json"
    malformed.write_text("{", encoding="utf-8")
    with caplog.at_level("WARNING"):
        keep_wav = bot._consume_voice_clone_pending("voice-file")

    assert keep_wav is None
    assert malformed.exists()
    assert "voice-clone state malformed" in caplog.text


def test_tg_voice_clone_pending_consume_failure_is_soft(tmp_path, caplog, monkeypatch):
    monkeypatch.setattr(bot, "STATE_DIR", tmp_path)
    monkeypatch.setattr(bot, "INSTANCE", "test")
    monkeypatch.setattr(bot.time, "time", lambda: 10)
    state_file = tmp_path / "voice-clone-pending-test.json"
    state_file.write_text(json.dumps({"expires_at": 20}), encoding="utf-8")

    def fail_rename(self: Path, target: Path):
        if self == state_file:
            raise OSError("rename failed")
        return original_rename(self, target)

    original_rename = Path.rename
    monkeypatch.setattr(Path, "rename", fail_rename)

    with caplog.at_level("WARNING"):
        keep_wav = bot._consume_voice_clone_pending("voice-file")

    assert keep_wav is None
    assert state_file.exists()
    assert "voice-clone state consume failed" in caplog.text


def test_tg_voice_clone_pending_logic_stays_out_of_handler():
    source = inspect.getsource(bot.on_voice)

    assert "_consume_voice_clone_pending" in source
    assert "json.loads" not in source
    assert "voice-clone-pending" not in source


def _split_bodies(parts: list[str]) -> list[str]:
    total = len(parts)
    bodies = []
    for idx, part in enumerate(parts):
        suffix = f" ({idx + 1}/{total})"
        assert part.endswith(suffix)
        bodies.append(part[: -len(suffix)])
    return bodies


def test_tg_handler_registration_keeps_transcript_sources_centralized():
    tree = ast.parse(Path(bot.__file__).read_text(encoding="utf-8"))
    main_func = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
    calls = [node for node in ast.walk(main_func) if isinstance(node, ast.Call)]
    sources = []
    for node in calls:
        if isinstance(node.func, ast.Name) and node.func.id in {"add_cmd", "add_cb", "add_msg"}:
            index = 1 if node.func.id != "add_cb" else 0
            arg = node.args[index]
            assert isinstance(arg, ast.Constant)
            sources.append(arg.value)

    assert sources == [
        "cmd_status",
        "cmd_context",
        "cmd_verbose",
        "cmd_cpu",
        "cmd_resume",
        "cmd_stop",
        "cmd_restart",
        "cmd_provider",
        "cmd_new",
        "cb_verbose",
        "cb_cpu",
        "cb_provider",
        "cb_codex",
        "cb_codex_add",
        "cb_codex_del",
        "cb_resume_channel",
        "cb_resume_back",
        "cb_resume",
        "cb_mcp",
        "text",
        "voice",
        "photo",
        "video",
        "document",
    ]

    assert sum(1 for node in calls if isinstance(node.func, ast.Name) and node.func.id == "_with_transcript") == 3


def test_tg_mcp_tool_descriptions_stay_operational():
    tools = {tool.name: tool for tool in asyncio.run(tg_mcp.list_tools())}
    text = tools["tg_send_text"]
    page = tools["tg_send_page"]
    voice = tools["tg_send_voice"]

    assert len(text.description) <= 180
    for marker in ("Telegram", "auto-delivered", "mid-turn pushes", "long-running progress", "proactive sends"):
        assert marker in text.description
    for marker in (
        "TG bot channel",
        "terminal/scheduled callers",
        "final turn text is already",
        "the user's",
    ):
        assert marker not in text.description

    assert len(page.description) <= 240
    for marker in ("Telegraph", "markdown", "long structured", "TG inline HTML", "returns the Telegraph URL"):
        assert marker in page.description
    for marker in ("Instant View card", "syntax-highlighted", "<ul>/<ol>", "h3/h4", "unsupported elements"):
        assert marker not in page.description
    title_description = page.inputSchema["properties"]["title"]["description"]
    assert title_description == "Page title"
    for marker in ("Instant View", "preview snippet"):
        assert marker not in title_description

    assert len(voice.description) <= 150
    for marker in ("voice message",):
        assert marker in voice.description
    for marker in (
        "arbitrary natural-language",
        "free combinations",
        "（笑）",
        "（咳嗽）",
        "（叹气）",
        "（停顿）",
    ):
        assert marker not in voice.description


def test_tg_mcp_voice_description_is_resolved_when_tools_are_listed(monkeypatch):
    monkeypatch.setenv("TTS_BACKEND", "mimo")
    monkeypatch.setenv("TTS_URL", "https://tts.example.test")

    tools = {tool.name: tool for tool in asyncio.run(tg_mcp.list_tools())}

    assert "<style> prefix" in tools["tg_send_voice"].description


def test_tg_instance_schema_stays_compact_without_losing_route_values():
    assert tg_mcp.INSTANCE_SCHEMA["enum"] == tg_mcp.TG_INSTANCES
    assert len(tg_mcp.INSTANCE_SCHEMA["description"]) <= 90
    for marker in ("Optional TG bot selector", "Omit", "bound channel"):
        assert marker in tg_mcp.INSTANCE_SCHEMA["description"]
    for label in tg_mcp.INSTANCE_LABELS.values():
        assert label not in tg_mcp.INSTANCE_SCHEMA["description"]


def test_tg_mcp_schema_helper_keeps_tool_contracts():
    tools = {tool.name: tool for tool in asyncio.run(tg_mcp.list_tools())}
    descriptions = [tool.description for tool in tools.values()]

    assert list(tools) == [
        "tg_send_buttons",
        "tg_send_text",
        "tg_send_file",
        "tg_send_album",
        "tg_send_location",
        "tg_send_voice",
        "tg_send_video",
        "tg_send_page",
    ]
    for marker in ("the user's", "to the user"):
        assert all(marker not in description for description in descriptions)
    required = {
        "tg_send_buttons": ["text", "options"],
        "tg_send_text": ["text"],
        "tg_send_file": ["path"],
        "tg_send_album": ["paths"],
        "tg_send_location": ["latitude", "longitude"],
        "tg_send_voice": ["text"],
        "tg_send_video": ["path"],
        "tg_send_page": ["title", "content_md"],
    }
    for name, tool in tools.items():
        schema = tool.inputSchema
        assert schema["type"] == "object"
        assert schema["required"] == required[name]
        assert schema["properties"]["instance"] is tg_mcp.INSTANCE_SCHEMA


def test_short_bubble_uses_html_parse_mode():
    parts, parse_mode = bot._format_bubble_parts("**ok**")

    assert parts == ["<b>ok</b>"]
    assert parse_mode == "HTML"


def test_to_html_preserves_tg_supported_blocks_and_escapes_content():
    text = "\n".join([
        "# Title <unsafe>",
        "> quote <x>",
        "[docs <label>](https://example.com/?q=<x>)",
        "`**literal**`",
        "<b>raw safe</b>",
        "```py",
        "print('<x>')",
        "```",
    ])

    rendered = bot._to_html(text)

    assert "<b>Title &lt;unsafe&gt;</b>" in rendered
    assert "<blockquote>quote &lt;x&gt;</blockquote>" in rendered
    assert '<a href="https://example.com/?q=&lt;x&gt;">docs &lt;label&gt;</a>' in rendered
    assert "<code>**literal**</code>" in rendered
    assert "<b>raw safe</b>" in rendered
    assert '<pre><code class="language-py">print(&#x27;&lt;x&gt;&#x27;)\n</code></pre>' in rendered


def test_long_bubble_falls_back_to_plain_chunks():
    text = "<b>" + ("x" * 5000) + "</b>"

    parts, parse_mode = bot._format_bubble_parts(text)

    assert parse_mode is None
    assert len(parts) > 1
    assert all(bot._utf16_len(part) <= bot._MAX_TG for part in parts)


def test_long_link_falls_back_to_plain_chunks():
    text = "[docs](https://example.com/" + ("a" * 5000) + ")"

    parts, parse_mode = bot._format_bubble_parts(text)

    assert parse_mode is None
    assert len(parts) > 1
    assert all(bot._utf16_len(part) <= bot._MAX_TG for part in parts)


def test_split_avoids_unpaired_inline_backtick_when_safe_boundary_exists():
    prefix = ("safe text " * 350).strip()
    text = f"{prefix} `" + ("x" * 1200) + "` tail"

    bodies = _split_bodies(bot._split(text))

    assert len(bodies) > 1
    assert (bodies[0].count("`") - bodies[0].count("\\`")) % 2 == 0
    assert bodies[1].lstrip().startswith("`")


def test_split_reopens_markdown_fence_across_chunks():
    text = "```py\n" + ("print('x')\n" * 500) + "```"

    bodies = _split_bodies(bot._split(text))

    assert len(bodies) > 1
    assert bodies[0].startswith("```py\n")
    assert bodies[0].endswith("\n```")
    assert bodies[1].startswith("```py\n")


def test_fmt_tool_skips_codex_internal_item_id():
    line = bot._fmt_tool(
        "/bin/zsh",
        {
            "id": "item_1",
            "type": "command_execution",
            "command": "/bin/zsh -lc 'echo ok'",
        },
    )

    assert line == "💻 Shell · echo"
    assert "/bin/zsh" not in line
    assert "item_1" not in line


def test_fmt_tool_marks_skill_usage_from_shell_command():
    line = bot._fmt_tool(
        "/bin/zsh",
        {
            "type": "command_execution",
            "command": (
                "/bin/zsh -lc \"sed -n '1,220p' "
                "~/cc-workspace/babata-skills/second-brain/SKILL.md\""
            ),
        },
    )

    assert line == "📚 Skill · second-brain"
    assert "sed -n" not in line


def test_fmt_tool_marks_memory_injection():
    line = bot._fmt_tool(
        "/bin/zsh",
        {
            "type": "command_execution",
            "command": (
                "~/cc-workspace/bin/babata-memory-context "
                "--profile lite --cpu codex --source terminal --include-top force"
            ),
        },
    )

    assert line == "🧠 Memory · inject lite (L0+daily-map) · codex/terminal · top force"


def test_fmt_tool_summarizes_shell_find_with_target_patterns():
    line = bot._fmt_tool(
        "/bin/zsh",
        {
            "type": "command_execution",
            "command": (
                "/bin/zsh -lc \"find ~/cc-workspace -maxdepth 4 "
                "\\( -iname '*light*' -o -iname '*home*' -o -iname '*hass*' "
                "-o -iname '*ha*' -o -iname '*mijia*' -o -iname '*yeelight*' \\) "
                "-not -path '*/node_modules/*' -print\""
            ),
        },
    )

    assert line == "📂 Find · cc-workspace · light/home/hass/ha/mijia/yeelight"
    assert "node_modules" not in line


def test_fmt_tool_summarizes_shell_file_reads_and_smart_home_commands():
    read_line = bot._fmt_tool(
        "/bin/zsh",
        {
            "type": "command_execution",
            "command": "/bin/zsh -lc \"sed -n '1,220p' ~/cc-workspace/skills-catalog/home/README.md\"",
        },
    )
    home_line = bot._fmt_tool(
        "/bin/zsh",
        {
            "type": "command_execution",
            "command": "~/cc-workspace/skills-catalog/home/smart-home/bin/ha light on",
        },
    )

    assert read_line == "📖 Read · home/README.md:1-220"
    assert home_line == "🏠 Smart-home · light on"


def test_fmt_tool_summarizes_common_operational_commands():
    assert bot._fmt_tool(
        "/bin/zsh",
        {"type": "command_execution", "command": "/bin/zsh -lc date"},
    ) == "🕑 Time · now"

    assert bot._fmt_tool(
        "/bin/zsh",
        {
            "type": "command_execution",
            "command": "python3 -m pytest tests/test_tg_comm_safety.py -q",
        },
    ) == "✅ Test · test_tg_comm_safety.py"

    assert bot._fmt_tool(
        "/bin/zsh",
        {
            "type": "command_execution",
            "command": (
                "for label in com.babata com.babata.vvv com.babata.vvvv com.babata.vvvvv; "
                "do DELAY=3 scripts/self-ops.sh restart \"$label\"; done"
            ),
        },
    ) == "🔁 Restart · TG bots"

    assert bot._fmt_tool(
        "/bin/zsh",
        {
            "type": "command_execution",
            "command": "sleep 8; launchctl list | rg 'com\\\\.babata'",
        },
    ) == "🚀 Launchd · list babata labels"

    assert bot._fmt_tool(
        "/bin/zsh",
        {
            "type": "command_execution",
            "command": "second-brain append-diary --date today",
        },
    ) == "📚 Skill · second-brain append diary"

    assert bot._fmt_tool(
        "/bin/zsh",
        {"type": "command_execution", "command": "ps -p 123,456 -o pid,command"},
    ) == "📋 Process · 2 pids"


def test_restart_reason_peek_survives_until_startup(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "STATE_DIR", tmp_path)

    bot._write_restart_reason("watchdog: poll heartbeat stale")

    assert bot._read_restart_reason() == "watchdog: poll heartbeat stale"
    assert bot._startup_restart_reason() == "watchdog: poll heartbeat stale"
    assert not (tmp_path / "restart-reason-com.babata.txt").exists()


def test_startup_restart_reason_is_never_omitted(monkeypatch, tmp_path):
    monkeypatch.setattr(bot, "STATE_DIR", tmp_path)

    assert bot._startup_restart_reason().startswith("未指定")


def test_fmt_tool_marks_subagent_and_web_search():
    assert bot._fmt_tool("Task", {"description": "review bot.py"}) == "👥 Subagent · review bot.py"
    assert bot._fmt_tool("WebSearch", {"query": "openclaw telegram progress"}) == (
        "🌐 WebSearch · openclaw telegram progress"
    )


def test_fmt_tool_keeps_current_task_tools_without_legacy_todowrite():
    assert "TodoWrite" not in bot._TOOL_EMOJI
    assert bot._fmt_tool("TaskCreate", {"name": "audit"}) == "✅ Task+ · audit"
    assert bot._fmt_tool("TaskUpdate", {"name": "audit"}) == "☑️ Task~ · audit"


class FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class FakeCallbackMessage:
    def __init__(self):
        self.message_id = 123
        self.text = "pick one"
        self.edits = []

    async def edit_message_text(self, text, **kwargs):
        self.edits.append((text, kwargs))


class FakeCallbackQuery:
    def __init__(self, user_id: int, data: str = "mcp:0:danger"):
        self.from_user = FakeUser(user_id)
        self.data = data
        self.message = FakeCallbackMessage()
        self.answers = []

    async def answer(self, text=None, **kwargs):
        self.answers.append((text, kwargs))

    async def edit_message_text(self, text, **kwargs):
        await self.message.edit_message_text(text, **kwargs)


class FakeCallbackUpdate:
    def __init__(self, query):
        self.callback_query = query


def test_callback_allowed_rejects_wrong_user(monkeypatch):
    async def run():
        monkeypatch.setattr(bot, "ALLOWED_USER", 42)
        query = FakeCallbackQuery(user_id=99)

        assert await bot._callback_allowed(query) is False
        assert query.answers == [("auth denied", {})]

    asyncio.run(run())


def test_button_callback_denies_before_bridge_or_process(monkeypatch):
    async def run():
        monkeypatch.setattr(bot, "ALLOWED_USER", 42)
        query = FakeCallbackQuery(user_id=99)
        update = FakeCallbackUpdate(query)

        def fail_resolve(*args, **kwargs):
            raise AssertionError("unauthorized callback reached bridge")

        async def fail_process(*args, **kwargs):
            raise AssertionError("unauthorized callback reached CC")

        monkeypatch.setattr(bot.bridge, "resolve", fail_resolve)
        monkeypatch.setattr(bot, "_process", fail_process)

        await bot.on_button_click(update, object())

        assert query.answers == [("auth denied", {})]
        assert query.message.edits == []

    asyncio.run(run())


class FakeBridgeBot:
    def __init__(self):
        self.messages = []

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return type("Sent", (), {"message_id": len(self.messages)})()


class FakeWriter:
    def __init__(self):
        self.data = b""

    def write(self, data: bytes):
        self.data += data

    async def drain(self):
        pass


def test_bridge_send_text_chunks_long_messages():
    async def run():
        fake_bot = FakeBridgeBot()
        br = tg_bridge.TGBridge()
        br.set_context(fake_bot, chat_id=7, reply_to=11)
        writer = FakeWriter()

        await br._handle_send_text({"text": "x" * 9000}, writer)

        assert len(fake_bot.messages) > 1
        assert all(
            tg_bridge._utf16_len(item["text"]) <= tg_bridge._TG_MAX_MESSAGE - 96
            for item in fake_bot.messages
        )
        assert all(item["reply_to_message_id"] == 11 for item in fake_bot.messages)
        response = json.loads(writer.data.decode())
        assert response["result"] == f"Text sent ({len(fake_bot.messages)} chunks)"

    asyncio.run(run())


def test_tg_mcp_open_bridge_retries_during_bot_restart(monkeypatch):
    tg_mcp = pytest.importorskip("tg_mcp")

    async def run():
        calls = 0

        async def fake_open(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise FileNotFoundError("bridge missing")
            return object(), FakeWriter()

        monkeypatch.setattr(tg_mcp.asyncio, "open_unix_connection", fake_open)
        monkeypatch.setattr(tg_mcp, "_BRIDGE_CONNECT_RETRY_SECONDS", 1.0)
        monkeypatch.setattr(tg_mcp, "_BRIDGE_CONNECT_RETRY_INTERVAL", 0.01)

        reader, writer = await tg_mcp._open_bridge("/tmp/missing.sock")

        assert reader is not None
        assert writer is not None
        assert calls == 2

    asyncio.run(run())
