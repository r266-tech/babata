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


def test_translate_tool_uses_explicit_url_without_site_arg(monkeypatch):
    monkeypatch.setenv("BABATA_SIDEBAR_MCP_SCOPE", "page-read")
    calls = []

    async def fake_translate_batch(target, batch, *, url):
        calls.append((target, batch, url))
        return [{"hash": batch[0]["hash"], "translated": "你好"}]

    monkeypatch.setattr(sidebar_mcp, "translate_batch", fake_translate_batch)

    result = asyncio.run(sidebar_mcp.call_tool("translate", {"text": "Hello", "target": "zh"}))

    assert result[0].text == "你好"
    assert len(calls) == 1
    target, batch, url = calls[0]
    assert target == "zh"
    assert url == "mcp://sidebar/translate"
    assert batch[0]["text"] == "Hello"


def test_suggest_prompts_targets_triggering_tab(monkeypatch):
    monkeypatch.setenv("BABATA_SIDEBAR_MCP_SCOPE", "proactive")
    calls = []

    async def fake_bridge_call(action, **payload):
        calls.append((action, payload))
        return {"ok": True, "result": "queued"}

    monkeypatch.setattr(sidebar_mcp, "_bridge_call", fake_bridge_call)

    result = asyncio.run(sidebar_mcp.call_tool(
        "suggest_prompts",
        {
            "prompts": ["继续看数据来源"],
            "tab_id": 1896461186,
            "window_id": 1896459597,
            "unexpected": "ignored",
        },
    ))

    assert result[0].text == "queued"
    assert calls == [(
        "notify_sw",
        {
            "name": "suggest_prompts",
            "args": {
                "prompts": ["继续看数据来源"],
                "tab_id": 1896461186,
                "window_id": 1896459597,
            },
        },
    )]
