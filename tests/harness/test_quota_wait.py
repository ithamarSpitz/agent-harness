from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from harness.config import HarnessConfig
from harness.quota import QuotaManager, QuotaResetInfo
from harness.state import StateStore


def _write_config(root: Path) -> HarnessConfig:
    path = root / "harness.yaml"
    path.write_text(
        "cursor: {models: {worker: null}}\n"
        "quota: {policy: wait, probe_interval_minutes: 60, reset_grace_minutes: 5}\n",
        encoding="utf-8",
    )
    return HarnessConfig.load(path)


def test_wait_until_codex_reset_plus_grace(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    state = StateStore(tmp_path / ".harness/state.json")
    now = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
    reset_at = now + timedelta(hours=3)

    def limits_source(_now: datetime) -> list[QuotaResetInfo]:
        return [QuotaResetInfo(provider="codex", reset_at=reset_at)]

    quota = QuotaManager(config, state, limits_source=limits_source)
    seconds = quota.next_wait_seconds(now=now)
    assert seconds == 3 * 3600 + 60
    assert quota.last_wait_message is not None
    assert "codex" in quota.last_wait_message
    assert reset_at.astimezone().strftime("%H:%M") in quota.last_wait_message
    assert "Waiting for codex quota until" in quota.last_wait_message


def test_earliest_reset_wins_when_both_providers_out(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    state = StateStore(tmp_path / ".harness/state.json")
    now = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
    codex_reset = now + timedelta(hours=5)
    cursor_reset = now + timedelta(hours=2)

    def limits_source(_now: datetime) -> list[QuotaResetInfo]:
        return [
            QuotaResetInfo(provider="codex", reset_at=codex_reset),
            QuotaResetInfo(provider="cursor", reset_at=cursor_reset),
        ]

    quota = QuotaManager(config, state, limits_source=limits_source)
    seconds = quota.next_wait_seconds(now=now)
    assert seconds == 2 * 3600 + 60
    assert quota.last_wait_message is not None
    assert "cursor" in quota.last_wait_message
    assert "codex" not in quota.last_wait_message


def test_limits_failure_uses_configured_interval(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    state = StateStore(tmp_path / ".harness/state.json")
    now = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)

    def limits_source(_now: datetime) -> list[QuotaResetInfo]:
        raise RuntimeError("limits unavailable")

    quota = QuotaManager(config, state, limits_source=limits_source)
    assert quota.next_wait_seconds(now=now) == 60 * 60
    assert quota.last_wait_message is None


def test_known_reset_sleep_capped_at_24_hours(tmp_path: Path) -> None:
    config = _write_config(tmp_path)
    state = StateStore(tmp_path / ".harness/state.json")
    now = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
    reset_at = now + timedelta(hours=48)

    def limits_source(_now: datetime) -> list[QuotaResetInfo]:
        return [QuotaResetInfo(provider="cursor", reset_at=reset_at)]

    quota = QuotaManager(config, state, limits_source=limits_source)
    assert quota.next_wait_seconds(now=now) == 24 * 3600
    assert quota.last_wait_message is not None
    assert "cursor" in quota.last_wait_message


def test_codex_latest_window_when_both_full(tmp_path: Path) -> None:
    """Both Codex windows full: wait until weekly (usable again), capped at 24h."""
    config = _write_config(tmp_path)
    state = StateStore(tmp_path / ".harness/state.json")
    now = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
    short_reset = now + timedelta(hours=2)
    weekly_reset = now + timedelta(days=3)

    def limits_source(_now: datetime) -> list[QuotaResetInfo]:
        return [
            QuotaResetInfo(provider="codex", reset_at=short_reset),
            QuotaResetInfo(provider="codex", reset_at=weekly_reset),
        ]

    quota = QuotaManager(config, state, limits_source=limits_source)
    assert quota.next_wait_seconds(now=now) == 24 * 3600
    assert quota.last_wait_message is not None
    assert "codex" in quota.last_wait_message
    assert weekly_reset.astimezone().strftime("%H:%M") in quota.last_wait_message
    # Duration wording should reflect the weekly horizon, not the 5-hour window.
    assert "in 3d" in quota.last_wait_message


def test_earliest_provider_usable_again_across_providers(tmp_path: Path) -> None:
    """Codex short window only + Cursor out later: wake at Codex usable-again."""
    config = _write_config(tmp_path)
    state = StateStore(tmp_path / ".harness/state.json")
    now = datetime(2030, 6, 1, 12, 0, tzinfo=timezone.utc)
    codex_reset = now + timedelta(hours=2)
    cursor_reset = now + timedelta(days=5)

    def limits_source(_now: datetime) -> list[QuotaResetInfo]:
        return [
            QuotaResetInfo(provider="codex", reset_at=codex_reset),
            QuotaResetInfo(provider="cursor", reset_at=cursor_reset),
        ]

    quota = QuotaManager(config, state, limits_source=limits_source)
    assert quota.next_wait_seconds(now=now) == 2 * 3600 + 60
    assert quota.last_wait_message is not None
    assert "codex" in quota.last_wait_message
    assert "cursor" not in quota.last_wait_message
