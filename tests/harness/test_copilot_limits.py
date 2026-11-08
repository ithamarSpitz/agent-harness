from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import harness.copilot_usage as copilot_usage
from harness.copilot_usage import CopilotQuota, CopilotUsageError
from harness.headroom import Headroom, headroom_snapshot
from harness.limits import LimitsPaths, render_limits, run_limits


def _paths(tmp_path: Path) -> LimitsPaths:
    return LimitsPaths(
        codex_home=tmp_path / "codex",
        claude_usage_cache=tmp_path / "claude" / "usage-cache.json",
        cursor_state_db=tmp_path / "cursor.vscdb",
        claude_home=tmp_path / "claude",
    )


RESET = datetime(2026, 12, 1, tzinfo=timezone.utc)


def _quota(*, command: list[str] | None) -> CopilotQuota:
    assert command == ["fake-gh", "quota"]
    return CopilotQuota("business", 200, 150, 75.0, False, RESET)


def test_limits_and_headroom_include_copilot_quota(tmp_path: Path) -> None:
    now = datetime(2026, 11, 8, tzinfo=timezone.utc)
    text = render_limits(
        _paths(tmp_path),
        now=now,
        copilot_enabled=True,
        copilot_quota_command=["fake-gh", "quota"],
        copilot_source=_quota,
    )
    payload = json.loads(
        render_limits(
            _paths(tmp_path),
            as_json=True,
            now=now,
            copilot_enabled=True,
            copilot_quota_command=["fake-gh", "quota"],
            copilot_source=_quota,
        )
    )
    snapshot = headroom_snapshot(
        paths=_paths(tmp_path),
        cache_path=tmp_path / "headroom.json",
        max_age_seconds=0,
        copilot_enabled=True,
        copilot_quota_command=["fake-gh", "quota"],
        copilot_source=_quota,
    )

    assert "Copilot (business)" in text
    assert "premium  25% used, 150 of 200 left, resets" in text
    assert payload["copilot"] == {
        "entitlement": 200,
        "percent_remaining": 75.0,
        "plan": "business",
        "remaining": 150,
        "resets_at": int(RESET.timestamp()),
    }
    assert snapshot["copilot"] == [
        Headroom("copilot", 25.0, int(RESET.timestamp()), "monthly", True)
    ]


def test_failure_is_unavailable_and_unknown(tmp_path: Path) -> None:
    def fail(*, command: list[str] | None) -> CopilotQuota:
        raise CopilotUsageError("quota offline")

    text = render_limits(_paths(tmp_path), copilot_enabled=True, copilot_source=fail)
    snapshot = headroom_snapshot(
        paths=_paths(tmp_path),
        cache_path=tmp_path / "failed.json",
        max_age_seconds=0,
        copilot_enabled=True,
        copilot_source=fail,
    )

    assert "Copilot: usage unavailable (quota offline)" in text
    assert snapshot["copilot"] == [
        Headroom("copilot", None, None, "monthly", False)
    ]


def test_explicit_enable_without_config_uses_default_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    commands: list[list[str] | None] = []

    def quota(*, command: list[str] | None) -> CopilotQuota:
        commands.append(command)
        return CopilotQuota("business", 200, 150, 75.0, False, RESET)

    run_limits(
        paths=_paths(tmp_path),
        copilot_enabled=True,
        copilot_source=quota,
        root=tmp_path,
    )
    snapshot = headroom_snapshot(
        paths=_paths(tmp_path),
        cache_path=tmp_path / "default-command.json",
        max_age_seconds=0,
        copilot_enabled=True,
        copilot_source=quota,
        root=tmp_path,
    )

    assert "Copilot (business)" in capsys.readouterr().out
    assert snapshot["copilot"][0].known is True
    assert commands == [None, None]


def test_disabled_copilot_is_absent(tmp_path: Path) -> None:
    text = render_limits(_paths(tmp_path), copilot_enabled=False)
    snapshot = headroom_snapshot(
        paths=_paths(tmp_path),
        cache_path=tmp_path / "disabled.json",
        max_age_seconds=0,
        copilot_enabled=False,
    )
    assert "Copilot" not in text
    assert "copilot" not in snapshot


def test_default_source_is_isolated() -> None:
    with pytest.raises(CopilotUsageError, match="isolated in tests"):
        copilot_usage.fetch_copilot_quota()
