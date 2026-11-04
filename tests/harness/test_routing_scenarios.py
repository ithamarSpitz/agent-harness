"""The five routing scenarios from the user's rules (Nov 4 2026), one test each.

Base order: agy, codex, cursor, claude. Full pace (injected): Codex 5-hour 30%/h,
Claude 5-hour 30%/h, weekly windows 3%/h, monthly 1%/h.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from harness.routing import ClaudeReserve, Choice, choose_provider

NOW = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
PRIORITY = ("agy", "codex", "cursor", "claude")
ALL = ("agy", "codex", "cursor", "claude")


def full_pace(provider: str, window: str) -> float:
    if provider == "cursor":
        return 1.0  # monthly pools
    if window == "5-hour":
        return 30.0  # Codex and Claude
    return 3.0  # weekly windows, including the agy pools


def w(used: float, hours: float | None = None) -> dict[str, object]:
    reset = None if hours is None else NOW + timedelta(hours=hours)
    return {"used_percent": used, "resets_at": reset}


DAY = 24.0


def choose(
    snapshot: dict[str, object],
    *,
    phase: str = "implement",
    enabled: tuple[str, ...] = ALL,
    implementer: str | None = None,
    disabled: set[str] | None = None,
) -> Choice:
    return choose_provider(
        snapshot,
        phase=phase,
        enabled=enabled,
        priority=PRIORITY,
        implementer_provider=implementer,
        disabled_for_task=disabled or set(),
        claude_reserve=ClaudeReserve(),
        now=NOW,
        pace=full_pace,
        auto_when_exhausted=True,
    )


def scenario_1_snapshot() -> dict[str, object]:
    return {
        "agy": {"windows": {"gemini_pool": w(40, 5 * DAY), "claude_gpt_pool": w(20, 5 * DAY)}},
        "codex": {"windows": {"five_hour": w(70, 3), "weekly": w(40, 4 * DAY)}},
        "cursor": {
            "windows": {"composer_grok": w(20, 20 * DAY), "other_models": w(30, 20 * DAY)}
        },
        "claude": {"windows": {"five_hour": w(10), "weekly": w(30)}},
    }


def test_scenario_1_everything_has_usage():
    snapshot = scenario_1_snapshot()

    implement = choose(snapshot)
    assert implement.provider == "agy"
    assert implement.reason == "order"
    review = choose(snapshot, phase="review", implementer="agy")
    assert review.provider == "codex"


def test_scenario_2_codex_about_to_waste():
    snapshot = scenario_1_snapshot()
    snapshot["codex"] = {"windows": {"five_hour": w(20, 1), "weekly": w(40, 4 * DAY)}}

    implement = choose(snapshot)
    assert implement.provider == "codex"
    assert implement.reason == "urgent: waste 50% of codex 5-hour"
    review = choose(snapshot, phase="review", implementer="codex")
    assert review.provider == "agy"


def test_scenario_3_agy_and_codex_urgent():
    snapshot = scenario_1_snapshot()
    snapshot["agy"] = {
        "windows": {"gemini_pool": w(30, 10), "claude_gpt_pool": w(20, 5 * DAY)}
    }
    snapshot["codex"] = {"windows": {"five_hour": w(50, 1), "weekly": w(40, 4 * DAY)}}

    implement = choose(snapshot)
    assert implement.provider == "agy"
    assert implement.reason == "urgent: waste 40% of agy gemini pool"
    review = choose(snapshot, phase="review", implementer="agy")
    assert review.provider == "codex"
    assert review.reason.startswith("urgent: waste 20% of codex 5-hour")


def test_scenario_4_cursor_pools_at_100():
    snapshot: dict[str, object] = {
        "agy": {"windows": {"gemini_pool": w(50, 5 * DAY)}},
        "codex": {"windows": {"five_hour": w(40, 3)}},
        "cursor": {
            "windows": {"composer_grok": w(100, 22 * DAY), "other_models": w(100, 22 * DAY)}
        },
    }
    enabled = ("agy", "codex", "cursor")

    attempt_1 = choose(snapshot, enabled=enabled)
    assert attempt_1.provider == "cursor"
    assert attempt_1.auto is True
    assert attempt_1.reason == "cursor auto: both pools at 100%"

    review = choose(snapshot, phase="review", enabled=enabled, implementer="cursor")
    assert review.provider == "agy"

    # Auto failed: Cursor is disabled for the task, so attempt 2 goes to agy.
    attempt_2 = choose(snapshot, enabled=enabled, disabled={"cursor"})
    assert attempt_2.provider == "agy"
    assert attempt_2.auto is False


def scenario_5_snapshot(claude_five_hour: float, codex_resets_in: float) -> dict[str, object]:
    return {
        "agy": {"windows": {"gemini_pool": w(100, 2 * DAY), "claude_gpt_pool": w(100, 2 * DAY)}},
        "codex": {"windows": {"five_hour": w(100, codex_resets_in)}},
        "cursor": {"windows": {"composer_grok": w(100), "other_models": w(100)}},
        "claude": {"windows": {"five_hour": w(claude_five_hour), "weekly": w(40)}},
    }


def test_scenario_5_only_claude_has_usage():
    cursor_auto_failed = {"cursor"}  # Auto quota-disabled for the task

    # H = 3 -> cap = 60 + 2/4 * 30 = 75%.
    implement = choose(scenario_5_snapshot(50, 3), disabled=cursor_auto_failed)
    assert implement.provider == "claude"
    assert "claude under cap 75%" in implement.reason

    # Claude at 76% is over the cap and Codex only returns in 3h: wait.
    waiting = choose(scenario_5_snapshot(76, 3), disabled=cursor_auto_failed)
    assert waiting.provider is None

    # Review when only Claude has usage: Claude (even if it implemented).
    review = choose(
        scenario_5_snapshot(50, 3), phase="review", implementer="claude",
        disabled=cursor_auto_failed,
    )
    assert review.provider == "claude"

    # H = 6 (nothing back for 6 hours): the cap is 90%, so 76% is fine.
    later = choose(scenario_5_snapshot(76, 6), disabled=cursor_auto_failed)
    assert later.provider == "claude"
    assert "claude under cap 90%" in later.reason
