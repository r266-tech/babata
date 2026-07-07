import asyncio
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_SDK_SITE = next(iter((_REPO / ".venv/lib").glob("python*/site-packages")), None)
if _SDK_SITE:
    sys.path.insert(0, str(_SDK_SITE))
sys.path.insert(0, str(_REPO))

import weixin_mcp
import weixin_ilink
import weixin_bridge


def _tools():
    return {tool.name: tool for tool in asyncio.run(weixin_mcp.list_tools())}


def test_wx_text_tool_description_stays_operational():
    tools = _tools()
    text = tools["wx_send_text"].description

    assert len(text) <= 140
    for marker in ("WeChat", "auto-delivered", "mid-turn pushes", "long-running progress", "proactive sends"):
        assert marker in text
    for marker in ("Markdown natively", "bold/italic", "lists/tables", "Bare URLs", "[text](url)"):
        assert marker not in text


def test_wx_tool_descriptions_stay_compact_and_schema_owned():
    tools = _tools()
    descriptions = [tool.description for tool in tools.values()]
    schema_descriptions = [
        prop["description"]
        for tool in tools.values()
        for prop in tool.inputSchema.get("properties", {}).values()
        if isinstance(prop, dict) and prop.get("description")
    ]

    assert sum(len(description) for description in descriptions) <= 390
    assert max(len(description) for description in descriptions) <= 140
    assert schema_descriptions == []
    forbidden_markers = (
        "the user's",
        "to the user",
        "babata's",
        "You are babata",
        "你是 babata",
        "共同进化",
        "身份认同",
    )
    for marker in forbidden_markers:
        assert all(marker not in description for description in descriptions)
    typing = tools["wx_send_typing"]
    assert typing.description == "Show/cancel WeChat typing indicator; status 1 on, 2 off."
    for marker in ("auto-cancels", "repeated calls are OK", "before a long task"):
        assert marker not in typing.description


def test_wx_tool_schema_keeps_supported_surface():
    tools = _tools()

    assert set(tools) == {
        "wx_send_text",
        "wx_send_image",
        "wx_send_video",
        "wx_send_file",
        "wx_send_typing",
    }
    assert tools["wx_send_text"].inputSchema["required"] == ["text"]
    assert tools["wx_send_image"].inputSchema["required"] == ["path"]
    assert (
        tools["wx_send_file"].inputSchema["properties"]["file_name"]["type"]
        == "string"
    )
    assert tools["wx_send_typing"].inputSchema["properties"]["status"]["enum"] == [
        1,
        2,
    ]


def test_wx_tool_schema_is_fresh_per_call():
    first = _tools()
    first["wx_send_typing"].inputSchema["properties"]["status"]["enum"].append(3)

    second = _tools()

    assert second["wx_send_typing"].inputSchema["properties"]["status"]["enum"] == [
        1,
        2,
    ]


def test_wx_voice_tool_stays_absent_from_model_visible_schema():
    tools = _tools()

    assert "wx_send_voice" not in tools
    assert not hasattr(weixin_ilink, "voice_item")
    assert not hasattr(weixin_bridge.WeixinBridge, "_handle_send_voice")
