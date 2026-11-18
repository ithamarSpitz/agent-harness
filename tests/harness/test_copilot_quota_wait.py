from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harness.copilot_usage import CopilotQuota
from harness.quota import default_limits_source


NOW = datetime(2026, 11, 18, tzinfo=timezone.utc)
RESET = NOW + timedelta(days=13)


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
        copilot=SimpleNamespace(
            enabled=enabled,
            quota_command=["gh", "api", "copilot_internal/user"],
        )
    )


def _quota(*, remaining: int, unlimited: bool = False) -> CopilotQuota:
    return CopilotQuota(
        plan="business",
        entitlement=200,
        remaining=remaining,
        percent_remaining=100.0 if remaining == 0 else 2.5,
        unlimited=unlimited,
        reset_time=RESET,
    )


def test_exhausted_copilot_adds_monthly_reset() -> None:
    calls: list[list[str] | None] = []

    def source(*, command: list[str] | None) -> CopilotQuota:
        calls.append(command)
        return _quota(remaining=0)

    found = default_limits_source(
        NOW,
        config=_config(enabled=True),
        copilot_source=source,
    )

    copilot = [item for item in found if item.provider == "copilot"]
    assert len(copilot) == 1
    assert copilot[0].reset_at == RESET
    assert calls == [["gh", "api", "copilot_internal/user"]]


def test_copilot_with_remaining_requests_adds_no_reset() -> None:
    def source(*, command: list[str] | None) -> CopilotQuota:
        return _quota(remaining=5)

    found = default_limits_source(
        NOW,
        config=_config(enabled=True),
        copilot_source=source,
    )

    assert not [item for item in found if item.provider == "copilot"]


def test_unlimited_copilot_adds_no_reset() -> None:
    def source(*, command: list[str] | None) -> CopilotQuota:
        return _quota(remaining=0, unlimited=True)

    found = default_limits_source(
        NOW,
        config=_config(enabled=True),
        copilot_source=source,
    )

    assert not [item for item in found if item.provider == "copilot"]


def test_copilot_source_failure_adds_no_reset() -> None:
    def source(*, command: list[str] | None) -> CopilotQuota:
        raise RuntimeError("quota unavailable")

    found = default_limits_source(
        NOW,
        config=_config(enabled=True),
        copilot_source=source,
    )

    assert not [item for item in found if item.provider == "copilot"]


def test_disabled_copilot_never_calls_source() -> None:
    calls: list[list[str] | None] = []

    def source(*, command: list[str] | None) -> CopilotQuota:
        calls.append(command)
        return _quota(remaining=0)

    found = default_limits_source(
        NOW,
        config=_config(enabled=False),
        copilot_source=source,
    )

    assert not [item for item in found if item.provider == "copilot"]
    assert calls == []
