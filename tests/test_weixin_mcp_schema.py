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


def test_wx_voice_tool_stays_absent_from_model_visible_schema():
    tools = {tool.name: tool for tool in asyncio.run(weixin_mcp.list_tools())}

    assert "wx_send_voice" not in tools
