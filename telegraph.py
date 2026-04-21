"""Thin Telegraph client: markdown → Telegraph DOM nodes → createPage URL.

Telegraph (telegra.ph) is Telegram's own pastebin. TG clients auto-render
shared URLs as Instant View cards with full rich layout — headings, real
lists, blockquotes, syntax-highlighted code — beyond what TG's HTML
parse_mode can display inline.

Persists the access_token to ~/code/babata/.telegraph-token after first
createAccount so all pages share one author.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://api.telegra.ph"
TOKEN_FILE = Path(__file__).parent / ".telegraph-token"


# ── Token management ─────────────────────────────────────────────────────


def _post(method: str, data: dict) -> dict:
    """POST to Telegraph API. List/dict values are JSON-encoded."""
    encoded = {}
    for k, v in data.items():
        if isinstance(v, (list, dict)):
            encoded[k] = json.dumps(v, ensure_ascii=False)
        elif isinstance(v, bool):
            encoded[k] = "true" if v else "false"
        else:
            encoded[k] = str(v)
    body = urllib.parse.urlencode(encoded).encode()
    req = urllib.request.Request(f"{API}/{method}", data=body)
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.load(resp)
    if not payload.get("ok"):
        raise RuntimeError(f"Telegraph {method} failed: {payload.get('error')}")
    return payload["result"]


def _get_token() -> str:
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            return token
    result = _post("createAccount", {"short_name": "babata", "author_name": "babata"})
    token = result["access_token"]
    TOKEN_FILE.write_text(token)
    return token


# ── Markdown → Telegraph nodes ───────────────────────────────────────────
#
# Telegraph supports these tags (per https://telegra.ph/api#NodeElement):
#   block: p, h3, h4, hr, aside, blockquote, pre, iframe, figure
#   list:  ul, ol, li
#   inline: a, b, i, em, strong, u, s, code, br
#
# Notes:
#   - h1/h2 are reserved for the page title (createPage 'title' arg); the
#     deepest usable heading in the body is h3. We map `#`/`##` to h3 and
#     `###+` to h4, since the page title already serves as h1/h2.
#   - Tables aren't supported natively. Tables in md degrade to plain lines.


_INLINE_RX = re.compile(
    r"(?P<code>`[^`\n]+`)"
    r"|(?P<bold>\*\*[^*\n]+?\*\*)"
    r"|(?P<italic_star>(?<!\*)\*(?!\*)[^*\n]+?(?<!\*)\*(?!\*))"
    r"|(?P<italic_us>(?<!_)_(?!_)[^_\n]+?(?<!_)_(?!_))"
    r"|(?P<strike>~~[^~\n]+?~~)"
    r"|(?P<link>\[[^\]\n]+\]\([^)\n]+\))"
)


def _parse_inline(text: str) -> list:
    """Parse a line of text with inline markdown, returning Telegraph node list.

    Plain strings and inline-formatted dicts are mixed in order.
    """
    out: list = []
    pos = 0
    for m in _INLINE_RX.finditer(text):
        if m.start() > pos:
            out.append(text[pos:m.start()])
        raw = m.group()
        if m.group("code"):
            out.append({"tag": "code", "children": [raw[1:-1]]})
        elif m.group("bold"):
            out.append({"tag": "b", "children": [raw[2:-2]]})
        elif m.group("italic_star") or m.group("italic_us"):
            out.append({"tag": "i", "children": [raw[1:-1]]})
        elif m.group("strike"):
            out.append({"tag": "s", "children": [raw[2:-2]]})
        elif m.group("link"):
            lm = re.match(r"\[([^\]]+)\]\(([^)]+)\)", raw)
            if lm:
                out.append({
                    "tag": "a",
                    "attrs": {"href": lm.group(2)},
                    "children": [lm.group(1)],
                })
            else:
                out.append(raw)
        pos = m.end()
    if pos < len(text):
        out.append(text[pos:])
    return out


def _is_block_start(line: str) -> bool:
    s = line.strip()
    if not s:
        return True
    return bool(
        re.match(r"^#{1,6}\s+", s)
        or s.startswith("```")
        or s.startswith(">")
        or re.match(r"^[-*]\s+", s)
        or re.match(r"^\d+\.\s+", s)
        or re.match(r"^(-{3,}|\*{3,}|={3,})\s*$", s)
    )


def md_to_nodes(md: str) -> list:
    """Parse markdown to Telegraph node list. Safe fallback: always valid."""
    lines = md.split("\n")
    nodes: list = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()

        # Blank line
        if not stripped:
            i += 1
            continue

        # Horizontal rule
        if re.match(r"^(-{3,}|\*{3,}|={3,})\s*$", stripped):
            nodes.append({"tag": "hr"})
            i += 1
            continue

        # Heading
        hm = re.match(r"^(#{1,6})\s+(.+)$", stripped)
        if hm:
            level = len(hm.group(1))
            tag = "h3" if level <= 3 else "h4"
            nodes.append({"tag": tag, "children": _parse_inline(hm.group(2).strip())})
            i += 1
            continue

        # Fenced code block
        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            buf: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            if i < n:
                i += 1  # skip closing fence
            code = "\n".join(buf)
            if lang:
                nodes.append({
                    "tag": "pre",
                    "children": [
                        {"tag": "code", "attrs": {"class": f"language-{lang}"}, "children": [code]}
                    ],
                })
            else:
                nodes.append({"tag": "pre", "children": [code]})
            continue

        # Blockquote (consecutive `>` lines)
        if stripped.startswith(">"):
            qlines = []
            while i < n and lines[i].strip().startswith(">"):
                qlines.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            quote_text = "\n".join(qlines).strip()
            nodes.append({"tag": "blockquote", "children": _parse_inline(quote_text)})
            continue

        # Unordered list
        if re.match(r"^\s*[-*]\s+", line):
            items = []
            while i < n and re.match(r"^\s*[-*]\s+", lines[i]):
                item = re.sub(r"^\s*[-*]\s+", "", lines[i])
                items.append({"tag": "li", "children": _parse_inline(item)})
                i += 1
            nodes.append({"tag": "ul", "children": items})
            continue

        # Ordered list
        if re.match(r"^\s*\d+\.\s+", line):
            items = []
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                item = re.sub(r"^\s*\d+\.\s+", "", lines[i])
                items.append({"tag": "li", "children": _parse_inline(item)})
                i += 1
            nodes.append({"tag": "ol", "children": items})
            continue

        # Paragraph — accumulate consecutive non-block lines
        pbuf = [line.rstrip()]
        i += 1
        while i < n and lines[i].strip() and not _is_block_start(lines[i]):
            pbuf.append(lines[i].rstrip())
            i += 1
        para = " ".join(b.strip() for b in pbuf if b.strip())
        if para:
            nodes.append({"tag": "p", "children": _parse_inline(para)})

    return nodes


# ── Public API ───────────────────────────────────────────────────────────


def create_page(title: str, content_md: str, author_name: str = "babata") -> str:
    """Create a Telegraph page, return its public URL.

    Title becomes the h1 of the Instant View card (not repeated in the body).
    Body supports full markdown — heading / list / code / blockquote / inline
    emphasis / links. Unsupported elements (tables, h1/h2 in body) degrade to
    plain paragraphs.

    Raises RuntimeError on API failure.
    """
    token = _get_token()
    nodes = md_to_nodes(content_md)
    # Guarantee non-empty — Telegraph rejects empty content
    if not nodes:
        nodes = [{"tag": "p", "children": [content_md or "(empty)"]}]
    # Title has 256-char hard limit
    safe_title = (title or "babata")[:256]
    result = _post("createPage", {
        "access_token": token,
        "title": safe_title,
        "author_name": author_name,
        "content": nodes,
        "return_content": False,
    })
    return result["url"]
