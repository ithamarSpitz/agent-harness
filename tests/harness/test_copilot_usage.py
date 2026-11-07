from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from harness.copilot_usage import (
    CopilotQuota,
    CopilotUsageError,
    fetch_copilot_quota,
    parse_copilot_user,
)


FIXTURE = Path(__file__).parent / "fixtures" / "copilot-user-sample.json"


def test_parse_copilot_user_fixture() -> None:
    assert parse_copilot_user(FIXTURE.read_text(encoding="utf-8")) == CopilotQuota(
        plan="individual",
        entitlement=200,
        remaining=199,
        percent_remaining=99.5,
        unlimited=False,
        reset_time=datetime(2026, 12, 1, tzinfo=timezone.utc),
    )


def test_parse_missing_premium_interactions_raises_small_error() -> None:
    payload = {
        "copilot_plan": "individual",
        "quota_reset_date_utc": "2026-12-01T00:00:00Z",
        "quota_snapshots": {},
        "account_detail": "private value",
    }

    with pytest.raises(CopilotUsageError, match="missing premium_interactions") as caught:
        parse_copilot_user(json.dumps(payload))

    assert "private value" not in str(caught.value)


def test_parse_bad_json_raises_small_error() -> None:
    private_response = "not JSON: private account value"

    with pytest.raises(CopilotUsageError, match="invalid JSON") as caught:
        parse_copilot_user(private_response)

    assert private_response not in str(caught.value)


def test_fetch_uses_default_command_without_shell() -> None:
    fixture_text = FIXTURE.read_text(encoding="utf-8")

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv == ["gh", "api", "copilot_internal/user"]
        assert kwargs == {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "timeout": 30,
            "check": False,
            "shell": False,
        }
        return subprocess.CompletedProcess(argv, 0, fixture_text, "")

    assert fetch_copilot_quota(run=fake_run).remaining == 199


def test_fetch_nonzero_exit_uses_only_first_stderr_line() -> None:
    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            argv, 1, "private response", "authentication failed\nprivate detail"
        )

    with pytest.raises(CopilotUsageError, match="^authentication failed$") as caught:
        fetch_copilot_quota(run=fake_run)

    assert "private" not in str(caught.value)


def test_fetch_timeout_uses_only_first_stderr_line() -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(
            "gh", 3, output="private response", stderr="request timed out\nprivate detail"
        )

    with pytest.raises(CopilotUsageError, match="^request timed out$") as caught:
        fetch_copilot_quota(timeout=3, run=fake_run)

    assert "private" not in str(caught.value)
