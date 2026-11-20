from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import harness.headroom as headroom
from harness.headroom import Headroom, headroom_snapshot
from harness.kiro_usage import KiroQuota
from harness.limits import LimitsPaths
from harness.quota import QuotaResetInfo, default_limits_source


NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)
RESET = NOW + timedelta(days=10)


@pytest.fixture(autouse=True)
def isolate_existing_provider_limits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("harness.quota.load_codex", lambda *args, **kwargs: ([], {}))
    monkeypatch.setattr("harness.quota.load_cursor", lambda *args, **kwargs: ([], {}))


def _paths(tmp_path: Path) -> LimitsPaths:
    return LimitsPaths(
        codex_home=tmp_path / "codex",
        claude_usage_cache=tmp_path / "claude" / "usage-cache.json",
        cursor_state_db=tmp_path / "cursor.vscdb",
        claude_home=tmp_path / "claude",
    )


def _config(*, enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        copilot=SimpleNamespace(enabled=False, quota_command=[]),
        agy=SimpleNamespace(enabled=False, command="agy"),
        kiro=SimpleNamespace(
            enabled=enabled,
            quota_command=["fake-kiro", "/usage"],
        )
    )


def _quota(*, used_percent: float) -> KiroQuota:
    return KiroQuota(
        plan="KIRO FREE",
        used=used_percent / 2,
        limit=50,
        used_percent=used_percent,
        reset_time=RESET,
    )


def test_enabled_kiro_adds_monthly_headroom(tmp_path: Path) -> None:
    calls: list[list[str] | None] = []

    def source(*, command: list[str] | None) -> KiroQuota:
        calls.append(command)
        return _quota(used_percent=40.0)

    snapshot = headroom_snapshot(
        max_age_seconds=0,
        paths=_paths(tmp_path),
        cache_path=tmp_path / "headroom.json",
        kiro_enabled=True,
        kiro_quota_command=["fake-kiro", "/usage"],
        kiro_source=source,
    )

    assert snapshot["kiro"] == [
        Headroom("kiro", 40.0, int(RESET.timestamp()), "monthly", True)
    ]
    assert calls == [["fake-kiro", "/usage"]]


def test_failing_kiro_source_adds_unknown_headroom(tmp_path: Path) -> None:
    def source(*, command: list[str] | None) -> KiroQuota:
        raise OSError(command)

    snapshot = headroom_snapshot(
        max_age_seconds=0,
        paths=_paths(tmp_path),
        cache_path=tmp_path / "headroom.json",
        kiro_enabled=True,
        kiro_source=source,
    )

    assert snapshot["kiro"] == [Headroom("kiro", None, None, "monthly", False)]


@pytest.mark.parametrize("first_enabled", [False, True])
def test_kiro_enabled_state_is_part_of_memory_and_disk_cache_key(
    tmp_path: Path, first_enabled: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache_path = tmp_path / "headroom.json"
    calls = 0

    def source(*, command: list[str] | None) -> KiroQuota:
        nonlocal calls
        calls += 1
        return _quota(used_percent=25.0)

    common = {
        "paths": _paths(tmp_path),
        "cache_path": cache_path,
        "now": lambda: 1_800_000_000.0,
        "kiro_source": source,
    }
    first = headroom_snapshot(kiro_enabled=first_enabled, **common)
    monkeypatch.setattr(headroom, "_memory_snapshot", None)
    second = headroom_snapshot(kiro_enabled=not first_enabled, **common)

    assert ("kiro" in first) is first_enabled
    assert ("kiro" in second) is not first_enabled
    assert calls == 1


def test_exhausted_kiro_adds_monthly_quota_reset() -> None:
    calls: list[list[str] | None] = []

    def source(*, command: list[str] | None) -> KiroQuota:
        calls.append(command)
        return _quota(used_percent=100.0)

    found = default_limits_source(
        NOW,
        config=_config(enabled=True),
        kiro_source=source,
    )

    assert [item for item in found if item.provider == "kiro"] == [
        QuotaResetInfo("kiro", RESET)
    ]
    assert calls == [["fake-kiro", "/usage"]]


def test_available_or_disabled_kiro_adds_no_quota_reset() -> None:
    calls = 0

    def source(*, command: list[str] | None) -> KiroQuota:
        nonlocal calls
        calls += 1
        return _quota(used_percent=99.9)

    available = default_limits_source(
        NOW,
        config=_config(enabled=True),
        kiro_source=source,
    )
    disabled = default_limits_source(
        NOW,
        config=_config(enabled=False),
        kiro_source=source,
    )

    assert not [item for item in available + disabled if item.provider == "kiro"]
    assert calls == 1
