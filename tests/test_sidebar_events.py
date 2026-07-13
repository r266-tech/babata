import json

import sidebar_events


def test_sidebar_events_bounds_long_string_fields(monkeypatch, tmp_path):
    events_file = tmp_path / "events.jsonl"
    monkeypatch.setattr(sidebar_events, "EVENTS_FILE", events_file)
    monkeypatch.setattr(sidebar_events, "MAX_FIELD_TEXT", 24)
    url = "https://example.com/path?keep=this-url-for-grep"
    error = "error-" + ("e" * 80) + "-EVENT-TAIL"

    sidebar_events.append(
        url,
        "translate_fail",
        error=error,
        nested={"detail": error, "_meta": "kept"},
    )

    raw = events_file.read_text(encoding="utf-8")
    assert (tmp_path / "events.lock").is_file()
    assert "EVENT-TAIL" not in raw
    event = json.loads(raw)
    assert event["url"] == url
    assert event["error"].endswith("chars]")
    assert event["error_sha256"] == sidebar_events._sha256_text(error)
    assert event["error_bytes"] == len(error.encode("utf-8"))
    assert event["nested"]["detail"].endswith("chars]")
    assert event["nested"]["_meta"] == "kept"
    assert sidebar_events._grep_url(url)[0]["kind"] == "translate_fail"


def test_sidebar_events_truncate_is_idempotent(monkeypatch):
    monkeypatch.setattr(sidebar_events, "MAX_FIELD_TEXT", 8)
    truncated = sidebar_events._truncate_text("abcdefghijk")

    assert sidebar_events._truncate_text(truncated) == truncated


def test_client_trace_batch_is_separate_aggregated_and_sampled(monkeypatch, tmp_path):
    events_file = tmp_path / "events.jsonl"
    trace_file = tmp_path / "client-trace.jsonl"
    monkeypatch.setattr(sidebar_events, "EVENTS_FILE", events_file)
    monkeypatch.setattr(sidebar_events, "CLIENT_TRACE_FILE", trace_file)
    monkeypatch.setattr(sidebar_events, "CLIENT_TRACE_MAX_SAMPLES", 2)
    url = "https://example.com/article"

    sidebar_events.append(url, "chat_turn", message_sha256="abc")
    sidebar_events.append_client_trace_batch(
        url,
        [
            {"src": "node-a", "dec": "keep", "txt": "secret-one"},
            {"src": "node-a", "dec": "keep", "txt": "secret-two"},
            {"src": "node-b", "dec": "skip", "nested": {"ignored": True}},
            "invalid",
            {"src": "node-c", "dec": "translate"},
        ],
    )

    fact_lines = events_file.read_text(encoding="utf-8").splitlines()
    assert len(fact_lines) == 1
    assert json.loads(fact_lines[0])["kind"] == "chat_turn"
    assert [event["kind"] for event in sidebar_events._grep_url(url)] == ["chat_turn"]

    raw_trace = trace_file.read_text(encoding="utf-8")
    assert "secret-one" not in raw_trace
    assert "secret-two" not in raw_trace
    trace = json.loads(raw_trace)
    assert trace["kind"] == "client_trace_batch"
    assert trace["received_n"] == 5
    assert trace["processed_n"] == 5
    assert trace["valid_n"] == 4
    assert trace["invalid_n"] == 1
    assert trace["unique_n"] == 3
    assert trace["sampled_n"] == 2
    assert trace["unsampled_unique_n"] == 1
    assert trace["samples"] == [
        {"repeat_n": 2, "fields": {"src": "node-a", "dec": "keep"}},
        {"repeat_n": 1, "fields": {"src": "node-b", "dec": "skip"}},
    ]


def test_client_trace_keeps_only_current_file_and_one_rotation(monkeypatch, tmp_path):
    trace_file = tmp_path / "client-trace.jsonl"
    monkeypatch.setattr(sidebar_events, "CLIENT_TRACE_FILE", trace_file)

    sidebar_events.append_client_trace_batch(
        "https://example.com/first",
        [{"src": "first-" + ("x" * 300), "dec": "keep"}],
    )
    first_size = trace_file.stat().st_size
    monkeypatch.setattr(
        sidebar_events,
        "CLIENT_TRACE_FILE_LIMIT_BYTES",
        first_size + 10,
    )

    sidebar_events.append_client_trace_batch(
        "https://example.com/second",
        [{"src": "second-" + ("x" * 300), "dec": "keep"}],
    )
    sidebar_events.append_client_trace_batch(
        "https://example.com/third",
        [{"src": "third-" + ("x" * 300), "dec": "keep"}],
    )

    rotated = trace_file.with_name(trace_file.name + ".1")
    assert sorted(path.name for path in tmp_path.glob("client-trace.jsonl*")) == [
        "client-trace.jsonl",
        "client-trace.jsonl.1",
    ]
    assert "https://example.com/third" in trace_file.read_text(encoding="utf-8")
    assert "https://example.com/second" in rotated.read_text(encoding="utf-8")
    assert "https://example.com/first" not in (
        trace_file.read_text(encoding="utf-8") + rotated.read_text(encoding="utf-8")
    )
    assert trace_file.stat().st_size <= sidebar_events.CLIENT_TRACE_FILE_LIMIT_BYTES
    assert rotated.stat().st_size <= sidebar_events.CLIENT_TRACE_FILE_LIMIT_BYTES
