from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import harness.board as board_module
from harness.board import BoardSnapshot, render_board, snapshot_from_state
from harness.headroom import Headroom, headroom_snapshot
from harness.limits import LimitsPaths
from harness.models import SprintSpec, TaskSpec
from harness.state import StateStore

RESET = int(datetime(2026, 11, 4, 3, 11).timestamp())


def h(provider: str, window: str, pct: float | None) -> Headroom:
    if pct is None:
        return Headroom(provider, None, None, window, False)
    return Headroom(provider, pct, RESET, window, True)


def snap(extra: bool = False, unknown: bool = False) -> BoardSnapshot:
    data = {
        "codex": [h("codex", "five_hour", None if unknown else 96), h("codex", "weekly", 46)],
        "cursor": [h("cursor", "composer_grok", 100), h("cursor", "other_models", 25)],
        "claude": [h("claude", "five_hour", 58), h("claude", "weekly", 22)],
    }
    if extra:
        data["agy"] = [h("agy", "weekly_a", 40), h("agy", "weekly_b", 20)]
    return BoardSnapshot("S", "t", 1, 1, headroom=data)


def test_full_and_short() -> None:
    assert render_board(snap(), 120)[1] == (
        "codex 5h 96% (03:11) wk 46% · cursor 100%/25% · claude 5h 58% wk 22%"
    )
    assert render_board(snap(), 60)[1] == "cx 96/46 · cu 100/25 · cl 58/22"


def test_headroom_uses_ascii_separator_when_unicode_is_disabled() -> None:
    line = render_board(snap(), 120, unicode=False)[1]

    assert line == (
        "codex 5h 96% (03:11) wk 46% | cursor 100%/25% | claude 5h 58% wk 22%"
    )
    assert line.isascii()
    line.encode("ascii")


def test_extra_provider_fits() -> None:
    assert render_board(snap(extra=True), 60)[1] == "cx 96/46 · cu 100/25 · cl 58/22 · ag 40/20"
    assert "agy weekly_a 40% weekly_b 20%" in render_board(snap(extra=True), 200)[1]


def test_unknown() -> None:
    assert render_board(snap(unknown=True), 40)[1].startswith("cx ?/46")
    assert "5h ?%" in render_board(snap(unknown=True), 120)[1]


def test_without_headroom_unchanged() -> None:
    lines = render_board(BoardSnapshot("S", "t", 1, 1), 60)
    assert lines == ["S   t   stage 1/1"]


def _state_and_sprint(tmp_path: Path) -> tuple[StateStore, SprintSpec]:
    sprint = SprintSpec(
        id="S1",
        name="t",
        tasks={"S1-T1": TaskSpec(id="S1-T1", file=Path("tasks/a.md"), sprint="S1", stage=1, depends_on=[])},
    )
    return StateStore(tmp_path / "state.json"), sprint


NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def test_snapshot_uses_injected_source_in_provider_order(tmp_path: Path) -> None:
    state, sprint = _state_and_sprint(tmp_path)
    calls: list[int] = []

    def fake() -> dict[str, list[Headroom]]:
        calls.append(1)
        return {"claude": [h("claude", "five_hour", 1)], "codex": [h("codex", "five_hour", 2)]}

    snapshot = snapshot_from_state(
        state, sprint, NOW, headroom_source=fake, provider_order=["codex", "claude"]
    )
    assert calls == [1]
    assert snapshot.headroom is not None and list(snapshot.headroom) == ["codex", "claude"]


def test_snapshot_failed_source_means_no_line(tmp_path: Path) -> None:
    state, sprint = _state_and_sprint(tmp_path)

    def boom() -> dict[str, list[Headroom]]:
        raise RuntimeError("x")

    assert snapshot_from_state(state, sprint, NOW, headroom_source=boom).headroom is None
    assert snapshot_from_state(state, sprint, NOW, headroom_source=None).headroom is None


def test_live_source_refreshes_stale_data_through_headroom_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The live source goes through headroom_snapshot, which refreshes stale cache data."""
    state, sprint = _state_and_sprint(tmp_path)
    cache = tmp_path / "cache.json"
    stale = {
        name: [
            {"provider": name, "used_percent": 77.0, "resets_at": None, "window": w, "known": True}
            for w in windows
        ]
        for name, windows in {
            "codex": ("five_hour", "weekly"),
            "cursor": ("composer_grok", "other_models"),
            "claude": ("five_hour", "weekly"),
        }.items()
    }
    cache.write_text(json.dumps({"cached_at": 1, "providers": stale}), encoding="utf-8")
    control = tmp_path / "control.json"
    control.write_text(json.dumps({"cached_at": 1, "providers": stale}), encoding="utf-8")
    fresh = headroom_snapshot(cache_path=control, now=lambda: 2.0, agy_enabled=False, copilot_enabled=False)
    assert fresh["codex"][0].used_percent == 77.0  # fixture is a readable cache
    seen: list[str] = []

    def src(name: str):  # type: ignore[no-untyped-def]
        def load(paths):  # type: ignore[no-untyped-def]
            seen.append(name)
            return [], {"error": "x"}

        return load

    def live(*, config=None) -> dict[str, list[Headroom]]:  # type: ignore[no-untyped-def]
        return headroom_snapshot(
            codex_source=src("codex"),
            cursor_source=src("cursor"),
            claude_source=src("claude"),
            paths=LimitsPaths.from_env(),
            cache_path=cache,
            now=lambda: 10_000.0,
            agy_enabled=False,
            copilot_enabled=False,
        )

    monkeypatch.setattr(board_module, "headroom_snapshot", live)
    snapshot = snapshot_from_state(
        state, sprint, NOW, headroom_source=board_module.live_headroom_source
    )
    assert sorted(seen) == ["claude", "codex", "cursor"]
    assert snapshot.headroom is not None
    assert render_board(snapshot, 40)[1] == "cx ?/? · cu ?/? · cl ?/?"
    assert "77" not in render_board(snapshot, 120)[1]


def test_snapshot_without_source_has_no_headroom(tmp_path: Path) -> None:
    state, sprint = _state_and_sprint(tmp_path)
    snapshot = snapshot_from_state(state, sprint, NOW)
    assert snapshot.headroom is None
    assert render_board(snapshot, 60) == ["S1   t   stage 1/1", "Stage 1", "  S1-T1 PENDING try 0/2"]
