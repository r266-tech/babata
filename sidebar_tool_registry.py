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
            "Read url/title/selection/scroll/lang; no DOM; pass tab_id/window_id."
        ),
        "inputSchema": {"type": "object", "properties": _target_props()},
    },
    {
        "name": "dom_query",
        "dispatch": "bridge",
        "description": (
            "CSS query tab; body text limit 50; root/props narrow."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _target_props({
                "selector": {"type": "string"},
                "root": {"type": "string"},
                "limit": {"type": "integer"},
                "props": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            }),
        },
    },
    {
        "name": "dom_inject",
        "dispatch": "bridge",
        "description": (
            "insertAdjacentHTML for explicit annotations/UI only; translation uses /translate."
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
            "Set input/text/attr; fire input/change; explicit edit only; translation uses /translate."
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
            "Synthetic .click() first match; Not trusted input; captcha/OAuth may reject."
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
            "Visible UI map for clicking: refs, roles, names, selectors, rects."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _target_props({
                "limit": {"type": "integer"},
            }),
        },
    },
    {
        "name": "article_extract",
        "dispatch": "bridge",
        "description": (
            "Extract tab article text/metadata; don't shell/curl current tab."
        ),
        "inputSchema": {"type": "object", "properties": _target_props()},
    },
    {
        "name": "page_click_ref",
        "dispatch": "bridge",
        "description": (
            "Click page_snapshot ref; needs snapshot_id/ref; stale refs error."
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
        "description": "Navigate target tab to url.",
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
            "Translate text to target_lang, default zh; no DOM side effect."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "target_lang": {"type": "string"},
            },
            "required": ["text"],
        },
    },
    {
        "name": "suggest_prompts",
        "dispatch": "notify",
        "description": (
            "Push 1-2 short follow-up chips; [] clears."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompts": {
                    "type": "array",
                    "items": {"type": "string"},
                },
            },
            "required": ["prompts"],
        },
    },
    {
        "name": "mascot_speak",
        "dispatch": "notify",
        "description": (
            "Show <=40 zh char bubble; auto-dismiss 30s."
        ),
        "inputSchema": {
            "type": "object",
            "properties": _target_props({
                "text": {"type": "string"},
            }),
            "required": ["text"],
        },
    },
    {
        "name": "bookmarks_search",
        "dispatch": "bridge",
        "description": "Search bookmarks by title/url; default max 50.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "bookmarks_tree",
        "dispatch": "bridge",
        "description": "Return bookmark folders for choosing parent_id.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "bookmarks_create",
        "dispatch": "bridge",
        "description": (
            "Create bookmark; parent_id optional; use bookmarks_tree."
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
            "Query tabs by active/audible/pinned/window/url; returns id/state/group."
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
        "description": "Close tab_ids. Destructive: explicit clear ask or ask first.",
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
        "description": "Group tab_ids, optional title/color; returns group_id/count.",
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
                },
            },
            "required": ["tab_ids"],
        },
    },
    {
        "name": "history_search",
        "dispatch": "bridge",
        "description": (
            "Search history by url/title; start_ms/end_ms epoch, max 100."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "start_ms": {"type": "integer"},
                "end_ms": {"type": "integer"},
                "max_results": {"type": "integer"},
            },
            "required": ["text"],
        },
    },
]

BRIDGE_TOOL_NAMES = frozenset(t["name"] for t in SIDEBAR_TOOLS if t["dispatch"] == "bridge")
PROACTIVE_TOOL_NAMES = frozenset({
    "tab_metadata",
    "page_snapshot",
    "suggest_prompts",
    "mascot_speak",
})
READ_TOOL_NAMES = frozenset({
    "tab_metadata",
    "dom_query",
    "page_snapshot",
    "article_extract",
    "translate",
})
GLOBAL_READ_TOOL_NAMES = frozenset({
    "bookmarks_search",
    "bookmarks_tree",
    "tabs_query",
    "history_search",
})
TOOL_SCOPE_NAMES = {
    "full": frozenset(t["name"] for t in SIDEBAR_TOOLS),
    "page-read": READ_TOOL_NAMES,
    "read": READ_TOOL_NAMES | GLOBAL_READ_TOOL_NAMES,
    "proactive": PROACTIVE_TOOL_NAMES,
}


def _tool_names(scope: str | None = None) -> frozenset[str]:
    return TOOL_SCOPE_NAMES.get((scope or "page-read").strip().lower(), TOOL_SCOPE_NAMES["page-read"])


def tool_specs(scope: str | None = None) -> list[dict[str, Any]]:
    allowed = _tool_names(scope)
    return [
        {
            "name": tool["name"],
            "description": tool["description"],
            "inputSchema": deepcopy(tool["inputSchema"]),
        }
        for tool in SIDEBAR_TOOLS
        if tool["name"] in allowed
    ]
