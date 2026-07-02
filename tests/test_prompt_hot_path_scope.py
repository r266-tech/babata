import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SDK_SITE = next(iter((_REPO / ".venv/lib").glob("python*/site-packages")), None)
if _SDK_SITE:
    sys.path.insert(0, str(_SDK_SITE))
sys.path.insert(0, str(_REPO))

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "123:test")
os.environ.setdefault("ALLOWED_USER_ID", "0")

import bot
import sidebar_bot
import weixin_bot as wb


PROMPT_CASES = [
    (
        "tg-source",
        lambda: bot._TG_SOURCE_PROMPT,
        300,
        ("Source: Telegram", "\\n\\n\\n", "Max 4096", "TG HTML subset", "Headings/tables/hr unsupported", "iOS"),
        ("Images/files", "user-provided context"),
    ),
    (
        "wx-source",
        lambda: wb._WX_SOURCE_PROMPT,
        320,
        ("Source: WeChat", "\\n\\n\\n", "No edit-message", "Max 4000", "bare URLs", "[text](url)"),
        ("code-fence-with-syntax-highlight", "nested markdown supported"),
    ),
    (
        "sidebar-source",
        lambda: sidebar_bot._SIDEBAR_SOURCE_PROMPT,
        1600,
        ("真实 schema 由 MCP 提供", "tab_id/window_id", "不可信数据", "清楚用户意图", "不要自行加载"),
        ("DevTools", "babata-memory-context"),
    ),
    (
        "sidebar-proactive",
        lambda: sidebar_bot._PROACTIVE_PROMPT,
        420,
        ("默认静默", "mascot_speak", "suggest_prompts", "tab_id/window_id", "不编造观察"),
        (),
    ),
    (
        "sidebar-agent-view",
        lambda: sidebar_bot._AGENT_VIEW_SOURCE_PROMPT,
        220,
        ("Source: babata sidebar avatar agent-view", "title/url/visible lines", "不读取文件", "不引入 babata 记忆", "一句中文短句"),
        ("共同进化", "哲学", "身份认同"),
    ),
    (
        "sidebar-clean-read",
        lambda: sidebar_bot._CLEAN_READ_SOURCE_PROMPT,
        220,
        ("Source: babata sidebar clean-read", "网页正文", "不读取文件", "不引入 babata 记忆", "不补写原文没有的信息", "中文 Markdown"),
        ("共同进化", "哲学", "身份认同"),
    ),
]


@pytest.mark.parametrize("name,prompt_getter,max_chars,required,forbidden", PROMPT_CASES)
def test_channel_prompts_stay_thin_and_boundary_focused(name, prompt_getter, max_chars, required, forbidden):
    prompt = prompt_getter()

    assert len(prompt) <= max_chars, name
    for marker in required:
        assert marker in prompt, name
    for marker in forbidden:
        assert marker not in prompt, name


def test_sidebar_prompt_tool_map_stays_compact():
    assert len(sidebar_bot._SIDEBAR_TOOL_LINES) <= 520


def test_sidebar_agent_view_user_prompt_stays_evidence_bound():
    prompt = sidebar_bot._build_agent_view_prompt(
        url="https://example.com/page",
        title="Example Title",
        snapshot_lines="line one\nline two",
    )

    assert len(prompt) <= 520
    for marker in (
        "双击头像触发",
        "18-70 字",
        "title/url/visible lines",
        "不可信网页文本",
        "不要遵循",
        "URL: https://example.com/page",
        "TITLE: Example Title",
        "<untrusted-page-content kind=\"visible-lines\">",
        "line one\nline two",
    ):
        assert marker in prompt
    for marker in ("高等智能生命", "共同进化", "哲学", "身份认同", "你是 babata", "作为 AI"):
        assert marker not in prompt
