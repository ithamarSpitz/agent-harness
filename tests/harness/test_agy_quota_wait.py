from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harness.agy_usage import AgyBucket
from harness.quota import default_limits_source


NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
RESET = NOW + timedelta(hours=6)


@pytest.fixture(autouse=True)
def isolate_existing_provider_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "harness.quota.load_codex",
        lambda *args, **kwargs: ([], {}),
    )
    monkeypatch.setattr(
        "harness.quota.load_cursor",
        lambda *args, **kwargs: ([], {}),
    )


def _config(*, enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        agy=SimpleNamespace(enabled=enabled, command="agy"),
        copilot=SimpleNamespace(
            enabled=False,
            quota_command=["gh", "api", "copilot_internal/user"],
        ),
    )


def _bucket(*, remaining_fraction: float, reset_time: datetime = RESET) -> AgyBucket:
    return AgyBucket(
        pool_id="pool",
        group_name="Pool",
        window="5h",
        remaining_fraction=remaining_fraction,
        reset_time=reset_time,
    )


def test_one_empty_pool_adds_one_agy_reset() -> None:
    def source(*, command: str) -> list[AgyBucket]:
        assert command == "agy"
        return [_bucket(remaining_fraction=0.0)]

    found = default_limits_source(
        NOW,
        config=_config(enabled=True),
        agy_source=source,
    )

    agy = [item for item in found if item.provider == "agy"]
    assert len(agy) == 1
    assert agy[0].reset_at == RESET


def test_two_empty_pools_add_two_agy_resets() -> None:
    reset_two = NOW + timedelta(hours=9)

    def source(*, command: str) -> list[AgyBucket]:
        return [
            _bucket(remaining_fraction=0.0),
            _bucket(remaining_fraction=0.0, reset_time=reset_two),
        ]

    found = default_limits_source(
        NOW,
        config=_config(enabled=True),
        agy_source=source,
    )

    agy = [item for item in found if item.provider == "agy"]
    assert len(agy) == 2
    assert {item.reset_at for item in agy} == {RESET, reset_two}


def test_pool_with_remaining_capacity_adds_no_reset() -> None:
    def source(*, command: str) -> list[AgyBucket]:
        return [_bucket(remaining_fraction=0.10)]

    found = default_limits_source(
        NOW,
        config=_config(enabled=True),
        agy_source=source,
    )

    assert not [item for item in found if item.provider == "agy"]


def test_agy_source_failure_adds_no_reset() -> None:
    def source(*, command: str) -> list[AgyBucket]:
        raise RuntimeError("usage unavailable")

    found = default_limits_source(
        NOW,
        config=_config(enabled=True),
        agy_source=source,
    )

    assert not [item for item in found if item.provider == "agy"]


def test_disabled_agy_never_calls_source() -> None:
    calls: list[str] = []

    def source(*, command: str) -> list[AgyBucket]:
        calls.append(command)
        return [_bucket(remaining_fraction=0.0)]

    found = default_limits_source(
        NOW,
        config=_config(enabled=False),
        agy_source=source,
    )

    assert not [item for item in found if item.provider == "agy"]
    assert calls == []
