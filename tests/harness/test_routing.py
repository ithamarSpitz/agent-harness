from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harness.routing import (
    ClaudeReserve,
    choose_provider,
    claude_five_hour_cap,
    claude_weekly_cap,
)


NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
RESERVE = ClaudeReserve()


def window(used: float, hours: float) -> dict[str, object]:
    return {"used_percent": used, "resets_at": NOW + timedelta(hours=hours)}


def choose(
    snapshot: dict[str, object],
    *,
    phase: str = "implement",
    enabled: object = ("codex", "cursor", "claude"),
    priority: tuple[str, ...] = ("codex", "cursor", "claude"),
    implementer_provider: str | None = None,
    disabled: object = (),
    auto_when_exhausted: bool = False,
):
    return choose_provider(
        snapshot,
        phase=phase,
        enabled=enabled,
        priority=priority,
        implementer_provider=implementer_provider,
        disabled_for_task=disabled,
        claude_reserve=RESERVE,
        now=NOW,
        auto_when_exhausted=auto_when_exhausted,
    )


@pytest.mark.parametrize(
    ("hours", "cap"),
    [(0.5, 60), (1, 60), (3, 75), (5, 90), (8, 90)],
)
def test_claude_five_hour_cap_follows_hours_until_another_provider(hours, cap):
    assert claude_five_hour_cap(hours, RESERVE) == pytest.approx(cap)


@pytest.mark.parametrize(("days", "cap"), [(5.65, 60.45), (3, 79), (0, 100), (20, 0)])
def test_claude_weekly_cap(days, cap):
    assert claude_weekly_cap(days, RESERVE) == pytest.approx(cap)


def test_claude_weekly_over_cap_means_no_usage():
    snapshot = {
        "claude": {"windows": {"five_hour": window(10, 4), "weekly": window(80, 72)}},
        "codex": {"primary": window(100, 2)},
    }
    choice = choose(snapshot, enabled=("codex", "claude"))
    assert choice.provider is None
    assert "claude weekly at 80% (cap 79%)" in choice.reason

    snapshot["claude"] = {"windows": {"five_hour": window(10, 4), "weekly": window(70, 72)}}
    assert choose(snapshot, enabled=("codex", "claude")).provider == "claude"


def test_claude_weekly_unknown_reset_gets_strictest_cap():
    # Unknown weekly reset: cap as if 7 days are left (100 - 7 * 7 = 51%).
    snapshot = {
        "claude": {
            "windows": {"five_hour": window(10, 4), "weekly": {"used_percent": 90}}
        },
        "codex": {"primary": window(100, 2)},
    }
    choice = choose(snapshot, enabled=("codex", "claude"))
    assert choice.provider is None
    assert "claude weekly at 90% (cap 51%)" in choice.reason

    snapshot["claude"] = {
        "windows": {"five_hour": window(10, 4), "weekly": {"used_percent": 52}}
    }
    assert choose(snapshot, enabled=("codex", "claude")).provider is None

    snapshot["claude"] = {
        "windows": {"five_hour": window(10, 4), "weekly": {"used_percent": 50}}
    }
    assert choose(snapshot, enabled=("codex", "claude")).provider == "claude"


def test_waste_goes_ahead_of_the_order():
    choice = choose(
        {
            "codex": {"primary": window(70, 3)},
            "cursor": {"monthly": window(20, 480)},
            "claude": {"windows": {"five_hour": window(10, 1)}},
        },
        priority=("codex", "cursor", "claude"),
    )

    # Claude: 90 left, can burn 30 in 1h -> waste 60; Codex: 30 left, can burn 90.
    assert choice.provider == "claude"
    assert choice.reason.startswith("urgent: waste 60% of claude 5-hour")


def test_all_out_names_every_provider():
    choice = choose(
        {
            "codex": {"primary": window(100, 2)},
            "cursor": {"monthly": window(100, 100)},
            "claude": {"windows": {"five_hour": window(80, 2)}},
        }
    )

    assert choice.provider is None
    assert all(provider in choice.reason for provider in ("codex", "cursor", "claude"))
    codex_reset_local = (NOW + timedelta(hours=2)).astimezone().strftime("%Y-%m-%d %H:%M")
    assert f"codex out until {codex_reset_local}" in choice.reason
    assert "cursor out until" in choice.reason
    assert "claude 5-hour at 80% (cap 67.5%)" in choice.reason


def test_no_headroom_data_follows_priority_exactly():
    choice = choose({}, enabled=("codex", "cursor"), priority=("cursor", "codex"))

    assert choice.provider == "cursor"
    assert choice.reason == "order"
    assert choice.scores == {"cursor": None, "codex": None}


def test_unknown_usage_is_tried_in_order_but_never_urgent():
    choice = choose(
        {"codex": {}, "claude": {"weekly": window(10, 1)}},
        enabled=("codex", "claude"),
        priority=("codex", "claude"),
    )

    # Claude's weekly window has 90% left and resets in 1h, but unknown Codex
    # usage (H = 0) is never urgent and Claude weekly is urgent here: waste wins.
    assert choice.provider == "claude"
    assert choice.reason.startswith("urgent: waste")

    quiet = choose({"codex": {}}, enabled=("codex", "claude"), priority=("codex", "claude"))
    assert quiet.provider == "codex"


def test_enabled_provider_omitted_from_priority_is_still_considered():
    choice = choose(
        {"cursor": {"monthly": window(20, 480)}},
        enabled={"codex": False, "cursor": {"enabled": True}},
        priority=("codex",),
    )

    assert choice.provider == "cursor"


def test_cursor_auto_goes_first_only_while_both_pools_are_at_100():
    both = {
        "cursor": {"windows": {"composer_grok": window(100, 480), "other_models": window(100, 480)}},
        "codex": {"primary": window(40, 3)},
    }
    below = {
        "cursor": {"windows": {"composer_grok": window(100, 480), "other_models": window(99, 480)}},
        "codex": {"primary": window(40, 3)},
    }

    auto = choose(both, auto_when_exhausted=True, enabled=("cursor", "codex"))
    assert (auto.provider, auto.auto) == ("cursor", True)
    assert auto.reason == "cursor auto: both pools at 100%"
    assert choose(both, enabled=("cursor", "codex")).provider == "codex"
    named = choose(below, auto_when_exhausted=True, enabled=("cursor", "codex"))
    assert named.provider == "codex"
    assert not named.auto
    assert choose(both, phase="review", auto_when_exhausted=True, enabled=("cursor", "codex")).provider == "codex"


def test_review_avoids_the_implementer_unless_it_is_the_only_provider_with_usage():
    snapshot = {"codex": {"primary": window(70, 3)}, "cursor": {"monthly": window(20, 480)}}

    other = choose(snapshot, phase="review", enabled=("codex", "cursor"), implementer_provider="codex")
    assert other.provider == "cursor"
    assert "codex implemented" in other.reason

    alone = choose(snapshot, phase="review", enabled=("codex",), implementer_provider="codex")
    assert alone.provider == "codex"

    snapshot["cursor"] = {"monthly": window(100, 480)}
    assert choose(
        snapshot, phase="review", enabled=("codex", "cursor"), implementer_provider="codex"
    ).provider == "codex"


def test_review_waits_when_nobody_has_usage():
    choice = choose(
        {"codex": {"primary": window(100, 3)}},
        phase="review",
        enabled=("codex",),
        implementer_provider="codex",
    )

    assert choice.provider is None


def test_provider_disabled_for_the_task_is_skipped():
    choice = choose({}, disabled={"codex"})

    assert choice.provider == "cursor"


def test_quota_disabled_provider_with_stale_capacity_keeps_claude_cap_strict():
    choice = choose(
        {
            "codex": {"primary": window(95, 1.9)},
            "cursor": {"monthly": window(100, 22 * 24)},
            "claude": {"windows": {"five_hour": window(60, 0.5)}},
        },
        disabled={"codex"},
    )

    assert choice.provider is None
    assert "claude 5-hour at 60% (cap 60%)" in choice.reason


def test_claude_cap_reset_is_an_earlier_wait_candidate():
    choice = choose(
        {
            "codex": {"primary": window(100, 1.5)},
            "cursor": {"monthly": window(100, 22 * 24)},
            "claude": {
                "windows": {"five_hour": window(65, 1), "weekly": window(10, 24)}
            },
        }
    )

    assert choice.provider is None
    assert choice.wait_until == NOW + timedelta(hours=1)
    assert "claude 5-hour at 65%" in choice.reason


def test_claude_weekly_cap_waits_until_cap_reaches_current_usage():
    choice = choose(
        {
            "claude": {
                "windows": {
                    "five_hour": window(10, 4),
                    "weekly": window(76, 3.875 * 24),
                }
            }
        },
        enabled=("claude",),
    )

    expected = NOW + timedelta(days=3.875 - 24 / 7)
    assert choice.provider is None
    assert choice.wait_until == pytest.approx(expected, abs=timedelta(minutes=1))


def test_claude_weekly_cap_wait_is_clamped_before_reset():
    choice = choose(
        {
            "claude": {
                "windows": {
                    "five_hour": window(10, 4),
                    "weekly": window(99, 2 * 24),
                }
            }
        },
        enabled=("claude",),
    )

    expected = NOW + timedelta(days=2 - 1 / 7)
    assert choice.provider is None
    assert choice.wait_until == pytest.approx(expected, abs=timedelta(minutes=1))
