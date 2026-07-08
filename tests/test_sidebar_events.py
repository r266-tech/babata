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
