from __future__ import annotations

import errno
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import harness.headroom as headroom
from harness.headroom import Headroom, headroom_snapshot
from harness.limits import LimitsPaths


def _paths(tmp_path: Path) -> LimitsPaths:
    return LimitsPaths(
        codex_home=tmp_path / "codex",
        claude_usage_cache=tmp_path / "claude" / "usage-cache.json",
        cursor_state_db=tmp_path / "cursor.vscdb",
        claude_home=tmp_path / "claude",
    )


def _source(payload: dict[str, Any], calls: list[str], name: str):
    def load(_paths: LimitsPaths) -> tuple[list[str], dict[str, Any]]:
        calls.append(name)
        return [], payload

    return load


def _snapshot(tmp_path: Path, calls: list[str], *, clock: list[float]):
    return headroom_snapshot(
        paths=_paths(tmp_path),
        cache_path=tmp_path / "headroom-cache.json",
        now=lambda: clock[0],
        codex_source=_source(
            {
                "primary": {"used_percent": 12, "resets_at": 1_800_000_100},
                "secondary": {"used_percent": 34.5, "resets_at": 1_800_000_200},
            },
            calls,
            "codex",
        ),
        cursor_source=_source(
            {
                "billing_cycle_end_ms": 1_800_000_300_000,
                "auto_percent_used": 56,
                "api_percent_used": 78,
            },
            calls,
            "cursor",
        ),
        claude_source=_source(
            {
                "windows": {
                    "five_hour": {
                        "used_percentage": 21,
                        "resets_at": 1_800_000_400,
                    },
                    "seven_day": {
                        "used_percentage": 43,
                        "resets_at": 1_800_000_500,
                    },
                }
            },
            calls,
            "claude",
        ),
    )


def test_fake_sources_produce_all_provider_windows(tmp_path: Path) -> None:
    calls: list[str] = []
    snapshot = _snapshot(tmp_path, calls, clock=[1_800_000_000.0])

    assert calls == ["codex", "cursor", "claude"]
    assert snapshot == {
        "codex": [
            Headroom("codex", 12.0, 1_800_000_100, "five_hour", True),
            Headroom("codex", 34.5, 1_800_000_200, "weekly", True),
        ],
        "cursor": [
            Headroom("cursor", 56.0, 1_800_000_300, "composer_grok", True),
            Headroom("cursor", 78.0, 1_800_000_300, "other_models", True),
        ],
        "claude": [
            Headroom("claude", 21.0, 1_800_000_400, "five_hour", True),
            Headroom("claude", 43.0, 1_800_000_500, "weekly", True),
        ],
    }


def test_fresh_memory_and_disk_caches_avoid_source_calls(tmp_path: Path) -> None:
    calls: list[str] = []
    clock = [1_800_000_000.0]
    expected = _snapshot(tmp_path, calls, clock=clock)
    calls.clear()

    assert _snapshot(tmp_path, calls, clock=clock) == expected
    assert calls == []


    # A different cache path evicts the process-local entry. Returning to the
    # first path therefore exercises the shared JSON cache.
    other = tmp_path / "other.json"
    headroom_snapshot(
        cache_path=other,
        paths=_paths(tmp_path),
        now=lambda: clock[0],
        codex_source=_source({}, calls, "other-codex"),
        cursor_source=_source({}, calls, "other-cursor"),
        claude_source=_source({}, calls, "other-claude"),
    )
    calls.clear()
    assert _snapshot(tmp_path, calls, clock=clock) == expected
    assert calls == []


@pytest.mark.parametrize("lock_kind", ["memory", "disk"])
@pytest.mark.parametrize("cache_update", ["new", "expired"])
def test_waiting_caller_checks_cache_with_current_time(
    tmp_path: Path, monkeypatch: Any, lock_kind: str, cache_update: str
) -> None:
    calls: list[str] = []
    clock = [1_800_000_000.0]
    expected = _snapshot(tmp_path, calls, clock=clock)
    calls.clear()
    cache_path = tmp_path / "headroom-cache.json"
    waiting = threading.Event()

    if lock_kind == "memory":
        original_lock = headroom._MEMORY_LOCK

        @contextmanager
        def observed_memory_lock():
            waiting.set()
            with original_lock:
                yield

        monkeypatch.setattr(headroom, "_MEMORY_LOCK", observed_memory_lock())
        held_lock = original_lock
    else:
        # Simulate a separate process with no local snapshot, sharing the real
        # filesystem lock and JSON cache with the writer below.
        monkeypatch.setattr(headroom, "_memory_snapshot", None)
        original_lock_file = headroom._lock_file

        def observed_disk_lock(lock_file: Any) -> None:
            waiting.set()
            original_lock_file(lock_file)

        held_lock = headroom._disk_lock(cache_path)

    with ThreadPoolExecutor(max_workers=1) as executor:
        with held_lock:
            if lock_kind == "disk":
                monkeypatch.setattr(headroom, "_lock_file", observed_disk_lock)
            future = executor.submit(_snapshot, tmp_path, calls, clock=clock)
            assert waiting.wait(timeout=5), "caller did not reach the cache lock"
            assert not future.done()
            clock[0] += 301
            if cache_update == "new":
                # Another caller publishes a snapshot after this caller began.
                frozen = headroom._freeze(expected)
                headroom._write_disk_cache(cache_path, clock[0], frozen)
                if lock_kind == "memory":
                    monkeypatch.setattr(headroom, "_memory_cached_at", clock[0])
        assert future.result(timeout=5) == expected

    assert calls == ([] if cache_update == "new" else ["codex", "cursor", "claude"])
    cache = json.loads(cache_path.read_text(encoding="utf-8"))
    assert cache["cached_at"] == clock[0]


def test_disk_lock_keeps_waiting_past_windows_retry_limit(
    tmp_path: Path, monkeypatch: Any
) -> None:
    attempts = 0

    def busy_then_lock(_lock_file: Any) -> None:
        nonlocal attempts
        attempts += 1
        if attempts <= 11:
            raise BlockingIOError(errno.EAGAIN, "lock busy")

    monkeypatch.setattr(headroom, "_try_lock_file", busy_then_lock)
    monkeypatch.setattr(headroom.time, "sleep", lambda _seconds: None)

    with (tmp_path / "cache.lock").open("a+b") as lock_file:
        headroom._lock_file(lock_file)

    assert attempts == 12


def test_old_cache_is_refreshed(tmp_path: Path) -> None:
    calls: list[str] = []
    clock = [1_800_000_000.0]
    _snapshot(tmp_path, calls, clock=clock)
    calls.clear()
    clock[0] += 301

    _snapshot(tmp_path, calls, clock=clock)

    assert calls == ["codex", "cursor", "claude"]
    cache = json.loads((tmp_path / "headroom-cache.json").read_text(encoding="utf-8"))
    assert cache["cached_at"] == clock[0]


def test_failing_source_is_unknown_and_does_not_block_others(tmp_path: Path) -> None:
    calls: list[str] = []

    def fail(_paths: LimitsPaths) -> tuple[list[str], dict[str, Any]]:
        calls.append("cursor")
        raise OSError("offline")

    snapshot = headroom_snapshot(
        max_age_seconds=0,
        paths=_paths(tmp_path),
        cache_path=tmp_path / "headroom-cache.json",
        now=lambda: 1_800_000_000.0,
        codex_source=_source(
            {
                "primary": {"used_percent": 1, "resets_at": 1_800_000_100},
                "secondary": {"used_percent": 2, "resets_at": 1_800_000_200},
            },
            calls,
            "codex",
        ),
        cursor_source=fail,
        claude_source=_source(
            {
                "windows": {
                    "five_hour": {"used_percentage": 3, "resets_at": 1_800_000_300},
                    "seven_day": {"used_percentage": 4, "resets_at": 1_800_000_400},
                }
            },
            calls,
            "claude",
        ),
    )

    assert calls == ["codex", "cursor", "claude"]
    assert all(not entry.known for entry in snapshot["cursor"])
    assert all(entry.used_percent is None for entry in snapshot["cursor"])
    assert all(entry.known for entry in snapshot["codex"] + snapshot["claude"])


def test_path_discovery_failure_returns_all_unknown(
    tmp_path: Path, monkeypatch: Any
) -> None:
    def fail_paths() -> LimitsPaths:
        raise OSError("environment unavailable")

    monkeypatch.setattr(LimitsPaths, "from_env", fail_paths)

    snapshot = headroom_snapshot(
        max_age_seconds=0,
        cache_path=tmp_path / "headroom-cache.json",
        now=lambda: 1_800_000_000.0,
    )

    assert set(snapshot) == {"codex", "cursor", "claude"}
    assert all(not entry.known for entries in snapshot.values() for entry in entries)
