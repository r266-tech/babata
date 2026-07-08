"""Telegram-facing tool status formatter.

This module is intentionally pure: it turns tool names/arguments into compact
Telegram status lines without depending on bot runtime state.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any


_TOOL_EMOJI = {
    "Bash": "\U0001f4bb",
    "Read": "\U0001f4d6",
    "Write": "\u270d\ufe0f",
    "Edit": "\U0001f527",
    "MultiEdit": "\U0001f527",
    "Glob": "\U0001f4c2",
    "Grep": "\U0001f50d",
    "WebFetch": "\U0001f4c4",
    "WebSearch": "\U0001f310",
    "Task": "\U0001f500",
    "TaskCreate": "\u2705",
    "TaskUpdate": "\u2611\ufe0f",
    "TaskGet": "\U0001f4cb",
    "TaskList": "\U0001f5c2\ufe0f",
    "NotebookEdit": "\U0001f4d3",
    "Skill": "\U0001f4da",
    "ToolSearch": "\U0001f9f0",
}

_SENSITIVE_TOOL_ARG = re.compile(
    r"(?:api[_-]?key|auth|bearer|credential|password|secret|token)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE = re.compile(
    r"\b(?:sk|ghp|gho|github_pat|xox[baprs]?)-[A-Za-z0-9_\-]{8,}",
    re.IGNORECASE,
)


def _compact_tool_text(value: Any, limit: int = 48) -> str:
    text = " ".join(str(value).split())
    if not text:
        return ""
    if _SENSITIVE_VALUE.search(text):
        return "[secret]"
    if len(text) > limit:
        return text[: max(1, limit - 3)].rstrip() + "..."
    return text


def _tool_line(icon: str, label: str, detail: str = "") -> str:
    detail = _compact_tool_text(detail, 72)
    return f"{icon} {label} · {detail}" if detail else f"{icon} {label}"


def _shell_label_icon(label: str) -> str:
    return {
        "Memory": "\U0001f9e0",
        "Skill": "\U0001f4da",
        "Search": "\U0001f50d",
        "Find": "\U0001f4c2",
        "List": "\U0001f4c2",
        "Read": "\U0001f4d6",
        "Smart-home": "\U0001f3e0",
        "Time": "\U0001f551",
        "Test": "\u2705",
        "Git": "\U0001f9fe",
        "Launchd": "\U0001f680",
        "Restart": "\U0001f501",
        "Process": "\U0001f4cb",
    }.get(label, "\U0001f4bb")


def _tool_arg(inp: dict, *keys: str, limit: int = 48) -> str:
    for key in keys:
        if _SENSITIVE_TOOL_ARG.search(key):
            continue
        value = inp.get(key)
        if value in (None, ""):
            continue
        if isinstance(value, (dict, list)):
            try:
                value = json.dumps(value, ensure_ascii=False, default=str)
            except Exception:
                value = str(value)
        preview = _compact_tool_text(value, limit)
        if preview:
            return preview
    return ""


def _codex_args(inp: dict) -> dict:
    raw = inp.get("arguments")
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _first_path_basename(text: str) -> str:
    matches = re.findall(r"(?:~|/)[^\s'\"\]\)]+", text)
    for match in matches:
        name = Path(match).name
        if name:
            return name
    return ""


def _short_shell_path(value: str) -> str:
    value = value.strip().strip("'\"").rstrip(",;")
    if not value:
        return ""
    if not (value.startswith(("/", "~", ".")) or "/" in value):
        return value
    parts = [p for p in value.replace("\\ ", " ").split("/") if p and p != "."]
    if not parts:
        return value
    if "skills-catalog" in parts:
        idx = parts.index("skills-catalog")
        tail = parts[idx + 1 :]
        return "/".join(tail[-3:]) if tail else "skills-catalog"
    if "cc-workspace" in parts:
        idx = parts.index("cc-workspace")
        tail = parts[idx:]
        return "/".join(tail[:2]) if len(tail) > 1 else "cc-workspace"
    if len(parts) >= 2 and parts[-1] in {"SKILL.md", "README.md"}:
        return "/".join(parts[-2:])
    return parts[-1]


def _join_preview(items: list[str], limit: int = 6) -> str:
    cleaned: list[str] = []
    for item in items:
        if item and item not in cleaned:
            cleaned.append(item)
    if not cleaned:
        return ""
    shown = cleaned[:limit]
    suffix = "/..." if len(cleaned) > limit else ""
    return "/".join(shown) + suffix


def _split_shell_parts(command: str) -> list[str]:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return [p for p in parts if p and not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", p)]


def _option_value(parts: list[str], option: str) -> str:
    if option not in parts:
        return ""
    idx = parts.index(option)
    return parts[idx + 1] if idx + 1 < len(parts) else ""


def _memory_context_detail(command: str) -> str:
    parts = _split_shell_parts(command)
    profile = _option_value(parts, "--profile")
    cpu = _option_value(parts, "--cpu")
    source = _option_value(parts, "--source")
    include_top = _option_value(parts, "--include-top")

    detail = "context"
    if profile:
        detail += f" {profile}"
    else:
        detail += " context"
    route = "/".join([p for p in (cpu, source) if p])
    if route:
        detail += f" · {route}"
    if include_top:
        detail += f" · top {include_top}"
    return detail


def _shell_find_detail(parts: list[str]) -> str:
    roots: list[str] = []
    patterns: list[str] = []
    idx = 1
    while idx < len(parts):
        item = parts[idx]
        if item in {"-name", "-iname", "-path", "-ipath"} and idx + 1 < len(parts):
            pattern = parts[idx + 1].strip("*")
            if pattern:
                patterns.append(pattern)
            idx += 2
            continue
        if item in {"-not", "!"}:
            if idx + 2 < len(parts) and parts[idx + 1] in {"-name", "-iname", "-path", "-ipath"}:
                idx += 3
            else:
                idx += 1
            continue
        if item.startswith("-") or item in {"(", ")", "!", "-o"}:
            idx += 2 if item in {"-maxdepth", "-mindepth", "-type"} else 1
            continue
        if not item.startswith(("!", "(")):
            roots.append(_short_shell_path(item))
        idx += 1
    root = ", ".join(roots[:2]) if roots else "files"
    wanted = _join_preview(patterns)
    return f"{root} · {wanted}" if wanted else root


def _shell_rg_detail(parts: list[str]) -> str:
    pattern = ""
    roots: list[str] = []
    skip_next = False
    option_args = {"-g", "--glob", "-t", "--type", "-T", "--type-not", "-A", "-B", "-C", "-m", "--max-count"}
    idx = 1
    while idx < len(parts):
        item = parts[idx]
        if skip_next:
            skip_next = False
            idx += 1
            continue
        if item in {"-e", "--regexp"} and idx + 1 < len(parts):
            pattern = pattern or parts[idx + 1]
            idx += 2
            continue
        if item in option_args:
            skip_next = True
            idx += 1
            continue
        if item.startswith("-"):
            idx += 1
            continue
        if not pattern:
            pattern = item
        else:
            roots.append(_short_shell_path(item))
        idx += 1
    terms = _join_preview([p for p in re.split(r"\|+", pattern) if p], 4) or pattern
    root = ", ".join(roots[:2])
    if len(roots) > 2:
        root += ", ..."
    return f"{terms} in {root}" if root else terms


def _shell_sed_detail(parts: list[str]) -> str:
    line_range = ""
    path = ""
    idx = 1
    while idx < len(parts):
        item = parts[idx]
        if item == "-n" and idx + 1 < len(parts):
            line_range = parts[idx + 1].rstrip("p").replace(",", "-")
            idx += 2
            continue
        if item.startswith("-"):
            idx += 1
            continue
        if "/" in item or item.startswith((".", "~")):
            path = _short_shell_path(item)
        idx += 1
    return f"{path}:{line_range}" if path and line_range else path or "stream"


def _shell_ls_detail(parts: list[str]) -> str:
    targets = [p for p in parts[1:] if not p.startswith("-")]
    if not targets:
        return "current dir"
    labels = [_short_shell_path(p) for p in targets]
    suffix = ", ..." if len(labels) > 2 else ""
    return ", ".join(labels[:2]) + suffix


def _shell_pytest_detail(parts: list[str]) -> str:
    try:
        idx = parts.index("pytest")
    except ValueError:
        return "pytest"
    targets = [p for p in parts[idx + 1 :] if p and not p.startswith("-")]
    if not targets:
        return "all tests"
    return ", ".join(_short_shell_path(p) for p in targets[:2])


def _self_ops_restart_detail(command: str) -> str:
    labels = re.findall(r"com\.babata(?:\.[A-Za-z0-9_-]+)?", command)
    if not labels:
        return "bot"
    if len(labels) >= 3 and all(label.startswith("com.babata") for label in labels):
        return "TG bots"
    return ", ".join(labels[:2]) + (", ..." if len(labels) > 2 else "")


def _launchctl_detail(parts: list[str], command: str) -> str:
    action = parts[1] if len(parts) > 1 else "launchctl"
    if "babata" in command:
        return f"{action} babata labels"
    label = next((p for p in parts[2:] if p.startswith("com.")), "")
    return f"{action} {label}".strip()


def _ps_detail(parts: list[str]) -> str:
    if "-p" in parts:
        idx = parts.index("-p")
        if idx + 1 < len(parts):
            pids = [p for p in parts[idx + 1].split(",") if p]
            return f"{len(pids)} pids" if len(pids) > 1 else f"pid {pids[0]}"
    return "processes"


def _skill_name_from_text(text: str) -> str:
    if "SKILL.md" not in text and "second-brain" not in text:
        return ""
    if "second-brain" in text:
        return "second-brain"
    parts = re.split(r"[\\/]+", text)
    for idx, part in enumerate(parts):
        if part == "SKILL.md" and idx > 0:
            return parts[idx - 1]
    return ""


def _unwrap_shell_command(command: str) -> str:
    command = _compact_tool_text(command, 4096)
    if not command or command == "[secret]":
        return command
    try:
        parts = shlex.split(command)
    except ValueError:
        return command
    shell_wrapper = len(parts) >= 3 and Path(parts[0]).name in {"bash", "sh", "zsh"}
    if shell_wrapper and parts[1] in {"-c", "-lc"}:
        return parts[2]
    return command


def _second_brain_detail(command: str) -> str:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    action = next(
        (Path(p).name for p in parts if not p.startswith("-") and Path(p).name != "second-brain"),
        "",
    )
    if "append-diary" in parts:
        action = "append diary"
    elif "read-diary" in parts:
        action = "read diary"
    elif "list-diaries" in parts:
        action = "list diaries"
    return "second-brain" + (f" {action}" if action else "")


def _shell_special_summary(command: str) -> tuple[str, str] | None:
    if "babata-memory-context" in command or "memory-inject.sh" in command:
        return "Memory", _memory_context_detail(command)
    if "babata-memory-reflex" in command:
        return "Memory", "reflex"
    if "self-ops.sh" in command and "restart" in command:
        return "Restart", _self_ops_restart_detail(command)
    if re.search(r"\blaunchctl\s+", command):
        segment = command[command.find("launchctl") :]
        segment = re.split(r"[|;]", segment, maxsplit=1)[0]
        return "Launchd", _launchctl_detail(_split_shell_parts(segment), command)
    if re.search(r"\bps\s+", command):
        segment = command[command.find("ps") :]
        segment = re.split(r"[|;]", segment, maxsplit=1)[0]
        return "Process", _ps_detail(_split_shell_parts(segment))

    skill_name = _skill_name_from_text(command)
    if skill_name and ("SKILL.md" in command or "second-brain" not in command):
        return "Skill", skill_name
    if "second-brain" in command:
        return "Skill", _second_brain_detail(command)
    return None


def _parsed_shell_summary(command: str, parts: list[str]) -> tuple[str, str]:
    if not parts:
        return "Shell", ""
    tool = Path(parts[0]).name
    if tool == "find":
        return "Find", _shell_find_detail(parts)
    if tool == "rg":
        return "Search", _shell_rg_detail(parts)
    if tool == "sed":
        return "Read", _shell_sed_detail(parts)
    if tool == "ls":
        return "List", _shell_ls_detail(parts)
    if tool == "date":
        return "Time", "now"
    if tool in {"ha", "smart-home"}:
        detail = " ".join(parts[1:4]).strip() or tool
        return "Smart-home", detail
    if tool == "launchctl":
        return "Launchd", _launchctl_detail(parts, command)
    if tool == "ps":
        return "Process", _ps_detail(parts)
    if tool == "git":
        return "Git", " ".join(parts[1:3]).strip() or "git"
    if tool == "pytest" or (tool in {"python", "python3"} and "-m" in parts and "pytest" in parts):
        return "Test", _shell_pytest_detail(parts)
    if tool in {"python", "python3", "uv", "npm", "pnpm", "yarn", "curl"}:
        detail = " ".join([tool, *parts[1:3]]).strip()
    else:
        detail = tool
    return "Shell", detail


def _shell_summary(command: str) -> tuple[str, str]:
    command = _unwrap_shell_command(command)
    if not command or command == "[secret]":
        return "Shell", command

    special = _shell_special_summary(command)
    if special is not None:
        return special
    return _parsed_shell_summary(command, _split_shell_parts(command))


def format_tool_status(name: str, inp: dict) -> str:
    name = str(name or "")
    inp = inp or {}
    codex_args = _codex_args(inp)
    merged = {**codex_args, **inp}
    lowered = name.lower()

    command = _tool_arg(merged, "command", "cmd", limit=4096)
    if lowered in {"/bin/zsh", "/bin/bash", "/bin/sh"} or merged.get("type") == "command_execution":
        label, detail = _shell_summary(command)
        return _tool_line(_shell_label_icon(label), label, detail)

    if "memory" in lowered or "session_search" in lowered or "ask_memory" in lowered:
        detail = _tool_arg(merged, "query", "target", "action", "source")
        return _tool_line("\U0001f9e0", "Memory", detail)

    if lowered in {"task", "delegate_task", "spawn_agent"} or "subagent" in lowered or lowered.startswith("subagent."):
        detail = _tool_arg(merged, "description", "goal", "task", "prompt", limit=56)
        return _tool_line("\U0001f465", "Subagent", detail)

    if lowered in {"websearch", "web_search", "search_query"} or "websearch" in lowered:
        detail = _tool_arg(merged, "query", "q", "search_query")
        return _tool_line("\U0001f310", "WebSearch", detail)

    if lowered in {"webfetch", "web_fetch", "web_extract"}:
        detail = _tool_arg(merged, "url", "urls")
        return _tool_line("\U0001f310", "WebFetch", detail)

    if lowered in {"skill", "skill_view", "skill_manage", "skills_list", "toolsearch", "tool_search"} or "skill" in lowered:
        detail = _tool_arg(merged, "name", "skill", "query", "category")
        return _tool_line("\U0001f4da", "Skill", detail)

    if lowered.startswith("browser_") or lowered.startswith("chrome_") or lowered in {"open", "click", "screenshot"}:
        detail = _tool_arg(merged, "url", "title", "selector", "ref", "path")
        return _tool_line("\U0001f5b1\ufe0f", "Browser", detail)

    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        detail = f"{parts[1]}/{parts[2]}" if len(parts) >= 3 else name
        return _tool_line("\U0001f9e9", "MCP", detail)

    display = {
        "Bash": "Shell",
        "Read": "Read",
        "Write": "Write",
        "Edit": "Edit",
        "MultiEdit": "Edit",
        "Glob": "Files",
        "Grep": "Search",
        "TaskCreate": "Task+",
        "TaskUpdate": "Task~",
        "TaskGet": "Task?",
        "TaskList": "Tasks",
        "NotebookEdit": "Notebook",
    }.get(name, name or "Tool")
    emoji = _TOOL_EMOJI.get(name, "\U0001f9f0")

    if name == "Bash" and command:
        label, detail = _shell_summary(command)
        return _tool_line(_shell_label_icon(label), label, detail)

    detail = _tool_arg(merged, "file_path", "path", "pattern", "query", "q", "url", "text", "name")
    if not detail:
        detail = _first_path_basename(json.dumps(merged, ensure_ascii=False, default=str))
    return _tool_line(emoji, display, detail)
