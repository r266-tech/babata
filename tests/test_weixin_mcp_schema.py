import asyncio
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SDK_SITE = next(iter((_REPO / ".venv/lib").glob("python*/site-packages")), None)
if _SDK_SITE:
    sys.path.insert(0, str(_SDK_SITE))
sys.path.insert(0, str(_REPO))

import weixin_mcp


def test_wx_text_tool_description_stays_operational():
    tools = {tool.name: tool for tool in asyncio.run(weixin_mcp.list_tools())}
    text = tools["wx_send_text"].description

    assert len(text) <= 220
    for marker in ("WeChat", "auto-delivered", "mid-turn pushes", "long-running progress", "proactive sends"):
        assert marker in text
    for marker in ("Markdown natively", "bold/italic", "lists/tables", "Bare URLs", "[text](url)"):
        assert marker not in text


def test_wx_tool_descriptions_stay_compact_and_schema_owned():
    tools = {tool.name: tool for tool in asyncio.run(weixin_mcp.list_tools())}
    descriptions = [tool.description for tool in tools.values()]

    assert max(len(description) for description in descriptions) <= 180
    typing = tools["wx_send_typing"]
    assert typing.description == "Show/cancel WeChat typing indicator."
    assert typing.inputSchema["properties"]["status"]["description"] == "1 = typing on, 2 = typing off"
    for marker in ("auto-cancels", "repeated calls are OK", "before a long task"):
        assert marker not in typing.description


def test_wx_voice_tool_stays_absent_from_model_visible_schema():
    tools = {tool.name: tool for tool in asyncio.run(weixin_mcp.list_tools())}

    assert "wx_send_voice" not in tools
