import json

import sidebar_history


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_sidebar_history_drops_records_before_last_boundary(monkeypatch, tmp_path):
    history_file = tmp_path / "chat_history.jsonl"
    monkeypatch.setattr(sidebar_history, "HISTORY_FILE", history_file)
    monkeypatch.setattr(sidebar_history, "HISTORY_TURN_RETENTION", 3)

    sidebar_history.append("user", "old question")
    sidebar_history.append("assistant", "old answer")
    sidebar_history.boundary()
    sidebar_history.append("user", "new question")
    sidebar_history.append("assistant", "new answer")

    raw = history_file.read_text(encoding="utf-8")
    assert "old question" not in raw
    assert "old answer" not in raw
    assert sidebar_history.read_since_last_boundary(limit=10) == [
        _rows(history_file)[1],
        _rows(history_file)[2],
    ]


def test_sidebar_history_retains_only_recent_turns_without_boundary(monkeypatch, tmp_path):
    history_file = tmp_path / "chat_history.jsonl"
    monkeypatch.setattr(sidebar_history, "HISTORY_FILE", history_file)
    monkeypatch.setattr(sidebar_history, "HISTORY_TURN_RETENTION", 3)

    for i in range(5):
        sidebar_history.append("user", f"question {i}")

    raw = history_file.read_text(encoding="utf-8")
    assert "question 0" not in raw
    assert "question 1" not in raw
    assert [row["text"] for row in sidebar_history.read_since_last_boundary(limit=10)] == [
        "question 2",
        "question 3",
        "question 4",
    ]
    assert len(sidebar_history.read_since_last_boundary(limit="bad")) == 3
    assert sidebar_history.read_since_last_boundary(limit=0) == []
