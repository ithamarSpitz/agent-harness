from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harness.routing import ClaudeReserve, choose_provider


NOW = datetime(2026, 11, 3, 12, tzinfo=timezone.utc)
RESERVE = ClaudeReserve(five_hour_max_used_percent=50, weekly_max_used_percent=80)


def window(used: float, hours: float) -> dict[str, object]:
    return {"used_percent": used, "resets_at": NOW + timedelta(hours=hours)}


def choose(
    snapshot: dict[str, object],
    *,
    phase: str = "implement",
    model_class: str = "worker",
    enabled: object = ("codex", "cursor", "claude"),
    priority: tuple[str, ...] = ("codex", "cursor", "claude"),
    implementer_provider: str | None = None,
):
    return choose_provider(
        snapshot,
        phase=phase,
        model_class=model_class,
        enabled=enabled,
        priority=priority,
        implementer_provider=implementer_provider,
        disabled_for_task=set(),
        claude_reserve=RESERVE,
        now=NOW,
    )


def test_claude_expiring_capacity_beats_more_distant_codex_capacity():
    choice = choose(
        {
            "claude": {"windows": {"weekly": window(22, 9)}},
            "codex": {"secondary": window(47, 6 * 24)},
        },
        enabled=("codex", "claude"),
    )

    assert choice.provider == "claude"
    assert choice.scores["claude"] == pytest.approx(78 / 9)
    assert "78% of the week free" in choice.reason


def test_claude_over_five_hour_reserve_is_excluded():
    choice = choose(
        {
            "claude": {
                "windows": {
                    "five_hour": window(55, 1),
                    "seven_day": window(10, 24),
                }
            },
            "codex": {"primary": window(90, 5)},
        },
        enabled=("claude", "codex"),
        priority=("claude", "codex"),
    )

    assert choice.provider == "codex"


@pytest.mark.parametrize(
    ("enabled", "expected"),
    [(("codex", "claude"), "claude"), (("codex",), "codex")],
)
def test_review_avoids_implementer_when_an_alternative_exists(enabled, expected):
    snapshot = {
        "codex": {"secondary": window(20, 12)},
        "claude": {"windows": {"seven_day": window(30, 12)}},
    }

    assert choose(
        snapshot,
        phase="review",
        enabled=enabled,
        implementer_provider="codex",
    ).provider == expected


def test_all_out_names_every_provider():
    choice = choose(
        {
            "codex": {"primary": window(100, 2)},
            "cursor": {"monthly": window(100, 100)},
            "claude": {"windows": {"five_hour": window(55, 2)}},
        }
    )

    assert choice.provider is None
    assert all(provider in choice.reason for provider in ("codex", "cursor", "claude"))
    codex_reset_local = (NOW + timedelta(hours=2)).astimezone().strftime("%H:%M")
    assert f"codex out until {codex_reset_local}" in choice.reason
    assert "cursor out until" in choice.reason


def test_unknown_headroom_uses_priority_order():
    choice = choose(
        {"codex": {}, "cursor": {}},
        enabled=("codex", "cursor"),
        priority=("cursor", "codex"),
    )

    assert choice.provider == "cursor"
    assert choice.scores == {"cursor": None, "codex": None}


def test_measured_urgency_beats_unknown_priority():
    choice = choose(
        {"codex": {}, "claude": {"weekly": window(10, 1)}},
        enabled=("codex", "claude"),
        priority=("codex", "claude"),
    )

    assert choice.provider == "claude"
    assert choice.scores["claude"] == pytest.approx(90.0)


def test_enabled_provider_omitted_from_priority_is_still_considered():
    choice = choose(
        {"cursor": {"monthly": window(20, 100)}},
        model_class="auto",
        enabled={"codex": False, "cursor": {"enabled": True}},
        priority=("codex",),
    )

    assert choice.provider == "cursor"
    assert "cursor/auto" in choice.reason


def test_cursor_auto_is_only_the_first_implementation_route():
    snapshot = {
        "cursor": {"monthly": window(99, 100)},
        "codex": {"secondary": window(50, 10)},
    }

    first = choose(snapshot, model_class="auto", enabled=("cursor", "codex"))
    later = choose(snapshot, model_class="worker", enabled=("cursor", "codex"))

    assert first.provider == "cursor"
    assert "cursor/auto" in first.reason
    assert later.provider == "codex"


def test_review_waits_when_cursor_auto_is_the_only_route():
    choice = choose(
        {"cursor": {"auto_only": True}},
        phase="review",
        model_class="auto",
        enabled=("cursor",),
    )

    assert choice.provider is None
    assert "cursor" in choice.reason
    assert "Auto" in choice.reason
