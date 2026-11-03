from __future__ import annotations

import json
from pathlib import Path

from harness.events import append_event, events_path, read_events


def test_append_and_read_newest_first(tmp_path: Path):
    append_event(tmp_path, "s1", "first")
    append_event(tmp_path, "s1", "second")
    append_event(tmp_path, "s1", "third")

    events = read_events(tmp_path, "s1", limit=10)
    assert [item["text"] for item in events] == ["third", "second", "first"]
    assert all(item["sprint"] == "s1" for item in events)
    assert all("time" in item for item in events)

    limited = read_events(tmp_path, "s1", limit=2)
    assert [item["text"] for item in limited] == ["third", "second"]


def test_read_events_filters_by_sprint(tmp_path: Path):
    append_event(tmp_path, "s1", "alpha")
    append_event(tmp_path, "s2", "beta")
    append_event(tmp_path, "s1", "gamma")

    assert [item["text"] for item in read_events(tmp_path, "s1", 10)] == ["gamma", "alpha"]
    assert [item["text"] for item in read_events(tmp_path, "s2", 10)] == ["beta"]


def test_events_file_trims_past_1000_lines(tmp_path: Path):
    path = events_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Seed 1000 lines, then one append triggers trim to last 500.
    seed = [
        json.dumps({"time": f"t{i:04d}", "sprint": "s1", "text": f"seed-{i}"}, sort_keys=True)
        for i in range(1000)
    ]
    path.write_text("\n".join(seed) + "\n", encoding="utf-8")
    append_event(tmp_path, "s1", "newest")

    lines = [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 500
    assert json.loads(lines[-1])["text"] == "newest"
    assert json.loads(lines[0])["text"] == "seed-501"

    newest = read_events(tmp_path, "s1", limit=1)
    assert newest[0]["text"] == "newest"
