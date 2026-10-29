import asyncio

import pytest

import sidebar_translate as st


def test_parse_marker_results_can_require_complete_ordered_batch():
    valid = "<<<RESULT 1>>>\n你好\n<<<RESULT 2>>>\n二\n<<<RESULT 3>>>\n三"
    assert st._parse_marker_results(valid, 3, require_complete=True) == ["你好", "二", "三"]
    assert st._parse_marker_results(
        "<<<RESULT 1>>>\n你好\n<<<RESULT 3>>>\n三", 3, require_complete=True
    ) == ["", "", ""]
    assert st._parse_marker_results(
        "<<<RESULT 1>>>\n你好\n<<<RESULT 2>>>\n二\n<<<RESULT 2>>>\n重复", 3, require_complete=True
    ) == ["", "", ""]
    assert st._parse_marker_results(
        "<<<RESULT 2>>>\n二\n<<<RESULT 1>>>\n你好\n<<<RESULT 3>>>\n三", 3, require_complete=True
    ) == ["", "", ""]


def test_protected_markers_require_same_tokens_and_valid_nesting():
    source = "[[BBT_LINK_1]]text [[BBT_FMT_2]]bold[[/BBT_FMT_2]][[/BBT_LINK_1]] [[BBT_KEEP_1]]"
    assert st._protected_markers_preserved(source, source)
    assert not st._protected_markers_preserved(source, source.replace("BBT_FMT_2", "BBT_FMT_3"))
    assert not st._protected_markers_preserved(source, source.replace("[[/BBT_LINK_1]]", ""))
    assert not st._protected_markers_preserved(source, source.replace("[[BBT_KEEP_1]]", ""))


def test_translation_provider_checks_batch_markers_chinese_and_number(monkeypatch):
    provider = {"base_url": "https://example.test/v1", "api_key": "test-key", "model": "test-model"}
    results = [
        "你好，世界。",
        "阅读 [[BBT_LINK_1]]简介[[/BBT_LINK_1]] 并 [[BBT_FMT_1]]继续学习[[/BBT_FMT_1]]。",
        "保持此代码不变：[[BBT_KEEP_1]]，数字是 42。",
    ]
    submitted = {}

    monkeypatch.setattr(st, "_resolve_provider", lambda _payload, require_model: provider)

    async def fake_translate(target, texts, *, url, provider, require_complete=False):
        submitted.update(target=target, texts=texts, url=url, require_complete=require_complete)
        return results

    monkeypatch.setattr(st, "_http_translate", fake_translate)
    response = asyncio.run(st.test_translation_provider({"model": "test-model"}))

    assert submitted == {
        "target": "zh",
        "texts": [
            "Hello, world.",
            "Read [[BBT_LINK_1]]the introduction[[/BBT_LINK_1]] and [[BBT_FMT_1]]keep learning[[/BBT_FMT_1]].",
            "Keep this code unchanged: [[BBT_KEEP_1]] and the number 42.",
        ],
        "url": "settings-test",
        "require_complete": True,
    }
    assert response == {
        "model": "test-model",
        "translated": results[0],
        "checked_items": 3,
        "marker_protocol": "LINK/FMT/KEEP",
    }


@pytest.mark.parametrize(
    ("results", "message"),
    [
        (["你好", "有效", ""], "complete batch"),
        (["Hello", "有效", "代码 42"], "no Chinese"),
        (["你好", "缺少 [[BBT_LINK_1]] 标记", "代码 [[BBT_KEEP_1]] 42"], "protected markers"),
        (
            [
                "你好",
                "阅读 [[BBT_LINK_1]]简介[[/BBT_LINK_1]] 并 [[BBT_FMT_1]]继续学习[[/BBT_FMT_1]]。",
                "代码 [[BBT_KEEP_1]] 43",
            ],
            "protected number",
        ),
    ],
)
def test_translation_provider_rejects_failed_capability_checks(monkeypatch, results, message):
    provider = {"base_url": "https://example.test/v1", "api_key": "test-key", "model": "test-model"}
    monkeypatch.setattr(st, "_resolve_provider", lambda _payload, require_model: provider)

    async def fake_translate(*_args, **_kwargs):
        return results

    monkeypatch.setattr(st, "_http_translate", fake_translate)
    with pytest.raises(st.TranslateConfigError, match=message):
        asyncio.run(st.test_translation_provider({"model": "test-model"}))
