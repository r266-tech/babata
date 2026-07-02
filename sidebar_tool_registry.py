"""Sidebar MCP tool schemas and compact prompt map."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


_TARGET_FIELDS: dict[str, dict[str, Any]] = {
    "tab_id": {
        "type": "integer",
        "description": "Target browser tab id from page_context. Prefer passing this for page tools.",
    },
    "window_id": {
        "type": "integer",
        "description": "Target browser window id from page_context. Fallback when tab_id is unavailable.",
    },
}


def _target_props(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**(extra or {}), **_TARGET_FIELDS}


SIDEBAR_TOOLS: list[dict[str, Any]] = [
    {
        "name": "tab_metadata",
        "dispatch": "bridge",
        "description": (
            "Read active/target tab meta: url, title, selection, scrollY, "
            "docHeight, lang. No DOM text. Use first to confirm V's page; pass "
            "tab_id/window_id to avoid active-tab races."
        ),
        "inputSchema": {"type": "object", "properties": _target_props()},
    },
    {
        "name": "dom_query",
        "dispatch": "bridge",
        "description": (
            "querySelectorAll on target tab. Defaults: selector body, props "
            "tag/text, limit 50; text cap 1500, html cap 2000. props: tag, id, "
            "class, text, html, href, value, name, type, placeholder, rect, "
            "attrs. root scopes inside an ancestor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _target_props({
                "selector": {"type": "string", "description": "CSS selector; default 'body'"},
                "root": {"type": "string", "description": "Scope querySelectorAll inside this ancestor (default document)"},
                "limit": {"type": "integer", "description": "Max results (default 50)"},
                "props": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Properties to extract. Default ['tag','text']",
                },
            }),
        },
    },
    {
        "name": "dom_inject",
        "dispatch": "bridge",
        "description": (
            "insertAdjacentHTML into matches. position: beforebegin, afterbegin, "
            "beforeend default, afterend. Use only for V-requested annotations/UI "
            "helpers; translation uses /translate. Returns {count}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _target_props({
                "selector": {"type": "string"},
                "html": {"type": "string"},
                "position": {
                    "type": "string",
                    "enum": ["beforebegin", "afterbegin", "beforeend", "afterend"],
                },
            }),
            "required": ["selector", "html"],
        },
    },
    {
        "name": "dom_set",
        "dispatch": "bridge",
        "description": (
            "Set input value, textContent, or attribute on matches; inputs fire "
            "input/change. Use for form filling or explicit page edits; "
            "translation uses /translate. Returns {count}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _target_props({
                "selector": {"type": "string"},
                "prop": {"type": "string"},
                "value": {"type": "string"},
            }),
            "required": ["selector", "prop", "value"],
        },
    },
    {
        "name": "dom_click",
        "dispatch": "bridge",
        "description": (
            "Synthetic .click() on first match. Returns {ok}. Not trusted input; "
            "captcha/OAuth buttons may refuse. Report that limit instead of "
            "claiming real user input."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _target_props({"selector": {"type": "string"}}),
            "required": ["selector"],
        },
    },
    {
        "name": "page_snapshot",
        "dispatch": "bridge",
        "description": (
            "Compact visible-page map for target tab: {snapshot_id, tab_id, "
            "window_id, url, title, items, lines}. Items include ref, role, name, "
            "selector, rect, is_new. Use before UI reasoning/clicking."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _target_props({
                "limit": {"type": "integer", "description": "Max visible elements, default 120"},
            }),
        },
    },
    {
        "name": "article_extract",
        "dispatch": "bridge",
        "description": (
            "Extract main readable content from current tab. Returns metadata, "
            "paragraph ids, plain text with [pN], markdown, char_count, "
            "extraction_method. Use for original page text; don't shell/curl "
            "current tab."
        ),
        "inputSchema": {"type": "object", "properties": _target_props()},
    },
    {
        "name": "page_click_ref",
        "dispatch": "bridge",
        "description": (
            "Click ref from page_snapshot on the same tab. Requires snapshot_id "
            "and ref. Reuses stored selector, scrolls/focuses if possible, then "
            "synthetic click. Returns {ok, selector, rect} or stale-ref."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _target_props({
                "snapshot_id": {"type": "string"},
                "ref": {"type": "string"},
            }),
            "required": ["snapshot_id", "ref"],
        },
    },
    {
        "name": "tab_navigate",
        "dispatch": "bridge",
        "description": "Navigate the target tab to url. Returns {ok, tab_id, url}.",
        "inputSchema": {
            "type": "object",
            "properties": _target_props({"url": {"type": "string"}}),
            "required": ["url"],
        },
    },
    {
        "name": "translate",
        "dispatch": "translate",
        "description": (
            "Translate plain text to target_lang (default zh). Pure text: no DOM "
            "read/inject/page side effect. Use for text translation or before "
            "deciding how to answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "target_lang": {"type": "string", "description": "Default zh"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "suggest_prompts",
        "dispatch": "notify",
        "description": (
            "Push 1-2 short follow-up chips to sidepanel after reading/answering. "
            "Use only when you can predict V's next move; [] clears."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "1–2 high-leverage short prompts (under 20 chars each ideal)",
                },
            },
            "required": ["prompts"],
        },
    },
    {
        "name": "mascot_speak",
        "dispatch": "notify",
        "description": (
            "Show a short babata speech bubble on V's page. Interrupt only for a "
            "real opinion, heads-up, or invitation. Keep <=40 zh chars; "
            "auto-dismiss 30s; click opens sidebar. Pass tab_id/window_id when "
            "known."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _target_props({
                "text": {"type": "string", "description": "Bubble text"},
            }),
            "required": ["text"],
        },
    },
    {
        "name": "bookmarks_search",
        "dispatch": "bridge",
        "description": (
            "Search V's bookmarks by free-text query (matches title/url). "
            "Returns array of {id, title, url, parent_id, date_added}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "description": "Default 50"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "bookmarks_tree",
        "dispatch": "bridge",
        "description": (
            "Return bookmark folder tree (folders only, no leaf bookmarks). "
            "Use to find an appropriate parent_id before bookmarks_create."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "bookmarks_create",
        "dispatch": "bridge",
        "description": (
            "Create a new bookmark. parent_id optional — defaults to top-level. "
            "Use bookmarks_tree first to find a sensible folder."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "url": {"type": "string"},
                "parent_id": {"type": "string"},
            },
            "required": ["title", "url"],
        },
    },
    {
        "name": "tabs_query",
        "dispatch": "bridge",
        "description": (
            "Query open tabs. Optional filters: active, audible, pinned, "
            "current_window, url pattern. Returns {id, url, title, active, "
            "audible, pinned, group_id, window_id, last_accessed}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "active": {"type": "boolean"},
                "audible": {"type": "boolean"},
                "pinned": {"type": "boolean"},
                "current_window": {"type": "boolean"},
                "url": {"type": "string"},
            },
        },
    },
    {
        "name": "tabs_close",
        "dispatch": "bridge",
        "description": (
            "Close tabs by id list. Destructive — use after V's clear ask "
            "('关掉所有包含关键词的 tabs') or after asking. Returns {closed: n}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
            },
            "required": ["tab_ids"],
        },
    },
    {
        "name": "tabs_group",
        "dispatch": "bridge",
        "description": (
            "Group tabs into a (possibly named/colored) tab group. color enum: "
            "grey/blue/red/yellow/green/pink/purple/cyan/orange. "
            "Returns {group_id, count}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "tab_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                },
                "group_title": {"type": "string"},
                "color": {"type": "string"},
            },
            "required": ["tab_ids"],
        },
    },
    {
        "name": "history_search",
        "dispatch": "bridge",
        "description": (
            "Search browsing history by url/title text. start_ms/end_ms are unix "
            "epoch ms (default 0..now). Returns {id, url, title, last_visit, "
            "visit_count}."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "start_ms": {"type": "integer"},
                "end_ms": {"type": "integer"},
                "max_results": {"type": "integer", "description": "Default 100"},
            },
            "required": ["text"],
        },
    },
]

_PROMPT_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("page read", ("tab_metadata", "page_snapshot", "article_extract", "dom_query")),
    ("page act", ("page_click_ref", "dom_click", "dom_set", "dom_inject", "tab_navigate")),
    ("ui notify", ("suggest_prompts", "mascot_speak")),
    ("text", ("translate",)),
    ("browser data", ("bookmarks_search", "bookmarks_tree", "bookmarks_create", "tabs_query", "tabs_group", "tabs_close", "history_search")),
)


BRIDGE_TOOL_NAMES = frozenset(t["name"] for t in SIDEBAR_TOOLS if t["dispatch"] == "bridge")


def tool_specs() -> list[dict[str, Any]]:
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": deepcopy(tool["inputSchema"]),
        }
        for tool in SIDEBAR_TOOLS
    ]


def prompt_tool_lines() -> str:
    available = {tool["name"] for tool in SIDEBAR_TOOLS}
    lines = []
    for label, names in _PROMPT_GROUPS:
        present = [name for name in names if name in available]
        if present:
            lines.append(f"- {label}: {', '.join(present)}")
    return "\n".join(lines)
