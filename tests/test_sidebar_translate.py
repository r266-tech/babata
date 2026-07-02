import asyncio

import httpx

import sidebar_translate as st


def test_extract_chat_content_reads_openai_compatible_message():
    resp = httpx.Response(
        200,
        json={"choices": [{"message": {"content": "  <<<RESULT 1>>>\n你好  "}}]},
    )

    assert st._extract_chat_content(resp) == "<<<RESULT 1>>>\n你好"


def test_extract_chat_content_rejects_bad_response_shape():
    resp = httpx.Response(200, json={"choices": [{"message": {"content": 42}}]})

    assert st._extract_chat_content(resp) is None


def test_translation_raw_content_retries_retryable_status(monkeypatch):
    calls: list[tuple[str, dict, dict]] = []

    class FakeClient:
        async def post(self, url, *, headers, json):
            calls.append((url, headers, json))
            if len(calls) == 1:
                return httpx.Response(429, text="busy")
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "<<<RESULT 1>>>\n你好"}}]},
            )

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(st, "_get_http_client", lambda: FakeClient())
    monkeypatch.setattr(st.asyncio, "sleep", no_sleep)

    provider = {
        "base_url": "https://example.test/api/v1",
        "api_key": "key",
        "model": "model",
    }
    raw = asyncio.run(
        st._translation_raw_content(
            provider,
            {"model": "model"},
            {"Authorization": "Bearer key"},
            url="https://page.test",
            target="zh",
        )
    )

    assert raw == "<<<RESULT 1>>>\n你好"
    assert len(calls) == 2
    assert calls[0][0] == "https://example.test/api/v1/chat/completions"


def test_http_translate_posts_prompt_and_parses_marker_results(monkeypatch):
    posted: dict[str, object] = {}

    class FakeClient:
        async def post(self, url, *, headers, json):
            posted.update({"url": url, "headers": headers, "json": json})
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "<<<RESULT 1>>>\n你好"}}]},
            )

    monkeypatch.setattr(st, "_get_http_client", lambda: FakeClient())

    provider = {
        "base_url": "https://example.test/api/v1",
        "api_key": "key",
        "model": "model",
    }

    translated = asyncio.run(
        st._http_translate(
            "zh", ["Hello"], url="https://page.test", provider=provider
        )
    )

    assert translated == ["你好"]
    assert posted["url"] == "https://example.test/api/v1/chat/completions"
    assert posted["headers"] == st._provider_headers("key")
    assert posted["json"]["model"] == "model"
    assert "<<<ITEM 1>>>\nHello" in posted["json"]["messages"][0]["content"]


def test_translation_raw_content_records_final_transport_failure(monkeypatch):
    events: list[tuple] = []

    class FakeClient:
        async def post(self, *_args, **_kwargs):
            raise httpx.RequestError("offline")

    async def no_sleep(_seconds):
        return None

    async def discard(_client=None):
        return None

    monkeypatch.setattr(st, "_get_http_client", lambda: FakeClient())
    monkeypatch.setattr(st, "_discard_http_client", discard)
    monkeypatch.setattr(st.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(
        st.sidebar_events, "append", lambda *args, **kwargs: events.append((args, kwargs))
    )

    provider = {
        "base_url": "https://example.test/api/v1",
        "api_key": "key",
        "model": "model",
    }
    raw = asyncio.run(
        st._translation_raw_content(
            provider,
            {"model": "model"},
            {"Authorization": "Bearer key"},
            url="https://page.test",
            target="zh",
        )
    )

    assert raw is None
    assert events == [
        (
            ("https://page.test", "translate_config_error"),
            {"reason": "offline", "target": "zh", "model": "model"},
        )
    ]


def test_validated_marker_results_preserves_all_empty_parse():
    assert st._validated_marker_results("not marker output", ["hello"], "model") == [""]
