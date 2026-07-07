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
        145,
        ("Source: Telegram", "\\n\\n\\n", "Max 4096", "TG HTML subset", "No headings/tables/hr"),
        ("Images/files", "user-provided context", "Progress bars", "▓", "iOS"),
    ),
    (
        "wx-source",
        lambda: wb._WX_SOURCE_PROMPT,
        165,
        ("Source: WeChat", "\\n\\n\\n", "No edit-message", "Max 4000", "bare URLs", "[text](url)"),
        ("code-fence-with-syntax-highlight", "nested markdown supported"),
    ),
    (
        "sidebar-source",
        lambda: sidebar_bot._SIDEBAR_SOURCE_PROMPT,
        525,
        ("MCP schema 为准", "tab_id/window_id", "不可信数据", "明确用户意图", "不要自行加载"),
        ("DevTools", "babata-memory-context", "同一个 babata"),
    ),
    (
        "sidebar-proactive",
        lambda: sidebar_bot._PROACTIVE_PROMPT,
        150,
        ("Source: babata sidebar proactive", "默认静默", "mascot_speak", "suggest_prompts", "tab_id/window_id", "不编造观察"),
        ("你是 babata", "轻量旁观通道", "共同进化", "哲学", "身份认同"),
    ),
    (
        "sidebar-agent-view",
        lambda: sidebar_bot._AGENT_VIEW_SOURCE_PROMPT,
        145,
        ("Source: babata sidebar avatar agent-view", "title/url/visible lines", "不读取文件", "不引入 babata 记忆", "一句中文短句"),
        ("共同进化", "哲学", "身份认同"),
    ),
    (
        "sidebar-clean-read",
        lambda: sidebar_bot._CLEAN_READ_SOURCE_PROMPT,
        125,
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
    assert len(sidebar_bot._SIDEBAR_TOOL_LINES) <= 300


@pytest.mark.parametrize(
    "intent,max_chars,required",
    [
        (
            "auto",
            70,
            ("SOURCE prompt", "翻译", "mascot_speak", "suggest_prompts", "静默"),
        ),
        (
            "prompt_suggestions",
            110,
            ("单击头像", "suggest_prompts", "1-2", "具体短 prompt", "不要回答", "prompts: []"),
        ),
        (
            "agent_view",
            115,
            ("双击头像", "一句中文锐评/学习建议", "mascot_speak", "page_snapshot", "页面内容"),
        ),
    ],
)
def test_sidebar_proactive_intent_instructions_stay_operational(intent, max_chars, required):
    instruction = sidebar_bot._proactive_intent_instruction(intent)

    assert len(instruction) <= max_chars
    for marker in required:
        assert marker in instruction
    for marker in ("高等智能", "高杠杆", "杠杆力", "prompt chips", "共同进化", "哲学", "身份认同"):
        assert marker not in instruction


def test_sidebar_agent_view_user_prompt_stays_evidence_bound():
    prompt = sidebar_bot._build_agent_view_prompt(
        url="https://example.com/page",
        title="Example Title",
        snapshot_lines="line one\nline two",
    )

    assert len(prompt) <= 360
    for marker in (
        "双击头像触发",
        "18-70 字中文锐评/学习建议",
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


def test_sidebar_clean_read_user_prompt_stays_structural_and_evidence_bound():
    prompt, truncated = sidebar_bot._build_clean_read_prompt(
        url="https://example.com/article",
        title="Example Article",
        article={
            "text": "p1 text\n\np2 text",
            "site_title": "Example Site",
            "byline": "Author",
            "published_at": "2026-07-02",
            "lang": "zh",
            "excerpt": "Excerpt",
            "char_count": 14,
            "paragraphs": ["p1 text", "p2 text"],
            "extraction_method": "readability",
        },
    )

    assert truncated is False
    assert len(prompt) <= 610
    for marker in (
        "三击头像触发",
        "不添加原文没有的事实",
        "不可信网页文本",
        "不要遵循",
        "只给中文 Markdown",
        "## 阅读判定",
        "## 核心意思",
        "## 净化正文",
        "## 保留的梗 / 好表达",
        "## AI 锐评",
        "## 原文依据",
        "\"url\":\"https://example.com/article\"",
        "<untrusted-page-content kind=\"article\" paragraph_ids=\"pN\">",
        "p1 text\n\np2 text",
    ):
        assert marker in prompt
    for marker in (
        "你是 babata",
        "无菌说明书",
        "顶级中文编辑",
        "情绪框架",
        "共同进化",
        "哲学",
        "身份认同",
        "标题党",
        "权威洗白",
        "char_count",
        "paragraph_count",
        "extraction_method",
    ):
        assert marker not in prompt
