import asyncio

import sidebar_mcp


def test_default_scope_lists_only_read_tools(monkeypatch):
    monkeypatch.delenv("BABATA_SIDEBAR_MCP_SCOPE", raising=False)

    tools = {tool.name for tool in asyncio.run(sidebar_mcp.list_tools())}

    assert "dom_query" in tools
    assert "article_extract" in tools
    assert "translate" in tools
    assert "dom_inject" not in tools
    assert "dom_set" not in tools
    assert "tab_navigate" not in tools
    assert "tabs_close" not in tools


def test_proactive_scope_lists_only_proactive_tools(monkeypatch):
    monkeypatch.setenv("BABATA_SIDEBAR_MCP_SCOPE", "proactive")

    tools = {tool.name for tool in asyncio.run(sidebar_mcp.list_tools())}

    assert tools == {"tab_metadata", "page_snapshot", "suggest_prompts", "mascot_speak"}


def test_proactive_scope_rejects_hidden_write_tools(monkeypatch):
    monkeypatch.setenv("BABATA_SIDEBAR_MCP_SCOPE", "proactive")

    result = asyncio.run(sidebar_mcp.call_tool("dom_inject", {"selector": "body", "html": "x"}))

    assert result[0].text == "Tool not available in this sidebar scope: dom_inject"
