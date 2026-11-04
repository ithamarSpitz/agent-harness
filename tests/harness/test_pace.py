from __future__ import annotations

import json
from pathlib import Path

from harness.headroom import Headroom
from harness.pace import full_pace, record_samples, waste

H = 3600
NOW = 100 * H


def write(path: Path, rows: list[tuple[float, float, int]], window: str = "five_hour") -> None:
    lines = [
        json.dumps(
            {"time": t, "provider": "codex", "window": window, "used_percent": u, "resets_at": r}
        )
        for t, u, r in rows
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_full_pace_picks_fastest_pair(tmp_path: Path) -> None:
    p = tmp_path / "h.jsonl"
    base = NOW - 3 * H
    write(p, [(base, 18, 5), (base + H, 48, 5), (base + H + 80 * 60 + 0, 96, 5)])
    # 48 -> 96 in 80 minutes = 36/h
    assert full_pace("codex", "five_hour", now=NOW, history_path=p) == 36


def test_reset_pairs_skipped(tmp_path: Path) -> None:
    p = tmp_path / "h.jsonl"
    base = NOW - 5 * H
    write(p, [(base, 90, 5), (base + H, 5, 5), (base + 2 * H, 10, 9), (base + 3 * H, 12, 9)])
    assert full_pace("codex", "five_hour", now=NOW, history_path=p) == 2


def test_defaults(tmp_path: Path) -> None:
    p = tmp_path / "none.jsonl"
    assert full_pace("codex", "five_hour", now=NOW, history_path=p) == 30
    assert full_pace("codex", "weekly", now=NOW, history_path=p) == 3
    assert full_pace("x", "monthly", now=NOW, history_path=p) == 1
    assert full_pace("x", "odd", now=NOW, history_path=p) == 3
    assert full_pace("codex", "five_hour", now=NOW, history_path=p, defaults={"five_hour": 7}) == 7


def test_waste() -> None:
    assert waste(80, 1, 30) == 50
    assert waste(30, 3, 30) == 0
    assert waste(70, 10, 3) == 40


def test_old_and_broken_lines(tmp_path: Path) -> None:
    p = tmp_path / "h.jsonl"
    write(p, [(NOW - 30 * H, 0, 5), (NOW - 29 * H, 90, 5)])
    with p.open("a", encoding="utf-8") as f:
        f.write("not json\n")
    assert full_pace("codex", "five_hour", now=NOW, history_path=p) == 30
    entry = Headroom("codex", 10.0, 5, "five_hour", True)
    record_samples([entry, Headroom("codex", None, None, "weekly", False)], now=NOW, path=p)
    rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]
    assert [r["time"] for r in rows] == [NOW]
    record_samples([entry], now=NOW + 60, path=p)
    assert len(p.read_text(encoding="utf-8").splitlines()) == 2


def test_record_failure_does_not_raise(tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    record_samples([Headroom("codex", 1.0, 5, "five_hour", True)], now=NOW, path=blocker / "h.jsonl")


def test_unknown_window_names_match_case_insensitively(tmp_path: Path) -> None:
    p = tmp_path / "h.jsonl"
    write(p, [(NOW - 2 * H, 10, 1), (NOW - H, 20, 1)], window="Mixed_Window")
    assert full_pace("codex", "mixed_window", now=NOW, history_path=p) == 10
    missing = tmp_path / "none.jsonl"
    assert full_pace("codex", "mixed_window", now=NOW, history_path=missing, defaults={"MIXED_window": 7}) == 7


def test_rewrite_limit_survives_new_process(tmp_path: Path) -> None:
    p = tmp_path / "h.jsonl"
    old = {"time": NOW - 30 * H, "provider": "codex", "window": "five_hour", "used_percent": 1, "resets_at": 1}
    p.write_text(json.dumps(old) + "\n", encoding="utf-8")
    entry = Headroom("codex", 5.0, 9, "five_hour", True)
    record_samples([entry], now=NOW, path=p)
    assert len(p.read_text(encoding="utf-8").splitlines()) == 1
    # within the hour: append only, even though state is only on disk
    p.write_text(p.read_text(encoding="utf-8") + json.dumps(old) + "\n", encoding="utf-8")
    record_samples([entry], now=NOW + 60, path=p)
    assert len(p.read_text(encoding="utf-8").splitlines()) == 3
    record_samples([entry], now=NOW + 3700, path=p)
    assert len(p.read_text(encoding="utf-8").splitlines()) == 3


def test_snapshot_records_to_harness_home(tmp_path: Path, monkeypatch) -> None:
    from typing import Any

    from harness import headroom
    from harness.limits import LimitsPaths

    monkeypatch.setenv("HARNESS_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(headroom, "_memory_snapshot", None)
    cache = tmp_path / "elsewhere" / "cache.json"
    paths = LimitsPaths(
        codex_home=tmp_path / "codex",
        claude_usage_cache=tmp_path / "claude.json",
        cursor_state_db=tmp_path / "cursor.vscdb",
        claude_home=tmp_path / "claude",
    )

    def codex(_paths: LimitsPaths) -> tuple[list[str], dict[str, Any]]:
        return [], {"primary": {"used_percent": 12, "resets_at": 1_800_000_100}}

    def empty(_paths: LimitsPaths) -> tuple[list[str], dict[str, Any]]:
        return [], {}

    headroom.headroom_snapshot(
        paths=paths, cache_path=cache, now=lambda: float(NOW),
        codex_source=codex, cursor_source=empty, claude_source=empty,
    )
    history = tmp_path / "home" / "headroom-history.jsonl"
    rows = [json.loads(line) for line in history.read_text(encoding="utf-8").splitlines()]
    assert [(r["provider"], r["used_percent"]) for r in rows] == [("codex", 12)]
    assert not (tmp_path / "elsewhere" / "headroom-history.jsonl").exists()


def test_schema_invalid_lines_skipped(tmp_path: Path) -> None:
    p = tmp_path / "h.jsonl"
    bad = [
        {"time": NOW - 2 * H, "provider": "codex", "window": "five_hour", "used_percent": 10},
        {"time": NOW - H, "provider": "codex", "window": "five_hour", "used_percent": 90},
        {"time": NOW - 2 * H, "provider": "codex", "window": "five_hour", "used_percent": 10, "resets_at": None},
        {"time": NOW - H, "provider": "codex", "window": "five_hour", "used_percent": 90, "resets_at": None},
        {"time": NOW - 2 * H, "provider": "codex", "window": "five_hour", "used_percent": "10", "resets_at": 5},
        {"time": NOW - H, "provider": "codex", "window": "five_hour", "used_percent": True, "resets_at": 5},
        {"time": NOW - H, "provider": 1, "window": "five_hour", "used_percent": 90, "resets_at": 5},
        {"time": NOW - H, "provider": "codex", "window": "five_hour", "used_percent": 90, "resets_at": 5.5},
        [1, 2, 3],
    ]
    p.write_text("\n".join(json.dumps(row) for row in bad) + "\n", encoding="utf-8")
    assert full_pace("codex", "five_hour", now=NOW, history_path=p) == 30
