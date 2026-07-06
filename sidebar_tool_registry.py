"""Sidebar MCP tool schemas and compact prompt map."""

from __future__ import annotations

from copy import deepcopy
from typing import Any


_TARGET_FIELDS: dict[str, dict[str, Any]] = {
    "tab_id": {
        "type": "integer",
    },
    "window_id": {
        "type": "integer",
    },
}


def _target_props(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {**(extra or {}), **_TARGET_FIELDS}


SIDEBAR_TOOLS: list[dict[str, Any]] = [
    {
        "name": "tab_metadata",
        "dispatch": "bridge",
        "description": (
            "Read tab meta only: url, title, selection, scroll/lang. No DOM "
            "text. Use first to confirm target page; pass tab_id/window_id."
        ),
        "inputSchema": {"type": "object", "properties": _target_props()},
    },
    {
        "name": "dom_query",
        "dispatch": "bridge",
        "description": (
            "querySelectorAll on target tab. Defaults selector=body, "
            "props=tag/text, limit=50. Use props/root to narrow output; "
            "text/html caps apply."
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
            "insertAdjacentHTML into matches. Use only for explicitly requested "
            "annotations/UI helpers; translation uses /translate. Returns {count}."
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
            "Set input value, textContent, or attribute; inputs fire input/change. "
            "Use for forms or explicit page edits; "
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
            "Synthetic .click() on first match. Not trusted input; captcha/OAuth "
            "may refuse. Report that limit instead of claiming real user input."
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
            "Visible-page map: refs, roles, names, selectors, rects, lines. "
            "Use before UI reasoning/clicking."
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
            "Extract readable article text from current tab: metadata, paragraph "
            "ids, markdown/text. Use original page text; don't shell/curl current tab."
        ),
        "inputSchema": {"type": "object", "properties": _target_props()},
    },
    {
        "name": "page_click_ref",
        "dispatch": "bridge",
        "description": (
            "Click page_snapshot ref on the same tab. Requires snapshot_id/ref; "
            "uses stored selector and returns stale-ref when invalid."
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
            "read/inject/page side effect."
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
            "Push 1-2 short follow-up chips when the next move is predictable; "
            "[] clears."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompts": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "1-2 short prompts; under 20 chars ideal",
                },
            },
            "required": ["prompts"],
        },
    },
    {
        "name": "mascot_speak",
        "dispatch": "notify",
        "description": (
            "Show <=40 zh char babata bubble for a real opinion, heads-up, or "
            "invitation. Auto-dismiss 30s; pass tab_id/window_id when known."
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
            "Search bookmarks by title/url text. Returns id, title, url, "
            "parent_id, date_added."
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
            "Return bookmark folders only. Use before bookmarks_create when "
            "choosing parent_id."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "bookmarks_create",
        "dispatch": "bridge",
        "description": (
            "Create a bookmark. parent_id optional, top-level by default; use "
            "bookmarks_tree when folder choice matters."
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
            "Query open tabs with optional active/audible/pinned/current_window/"
            "url filters. Returns tab identity, state, group, and last_accessed."
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
            "Close tabs by id list. Destructive: use after explicit clear ask or "
            "after asking. Returns {closed: n}."
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
            "Group tabs, optionally named/colored. color enum is in schema docs. "
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
                "color": {
                    "type": "string",
                    "enum": [
                        "grey",
                        "blue",
                        "red",
                        "yellow",
                        "green",
                        "pink",
                        "purple",
                        "cyan",
                        "orange",
                    ],
                    "description": "Tab group color",
                },
            },
            "required": ["tab_ids"],
        },
    },
    {
        "name": "history_search",
        "dispatch": "bridge",
        "description": (
            "Search browsing history by url/title text. start_ms/end_ms are unix "
            "epoch ms; returns url, title, last_visit, visit_count."
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
    ("read", ("tab_metadata", "page_snapshot", "article_extract", "dom_query")),
    ("act", ("page_click_ref", "dom_click", "dom_set", "dom_inject", "tab_navigate")),
    ("ui", ("suggest_prompts", "mascot_speak")),
    ("text", ("translate",)),
    ("data", ("bookmarks_search", "bookmarks_tree", "bookmarks_create", "tabs_query", "tabs_group", "tabs_close", "history_search")),
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
            lines.append(f"- {label}: {','.join(present)}")
    return "\n".join(lines)
