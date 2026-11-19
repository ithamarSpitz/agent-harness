from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from harness.kiro_usage import (
    KiroQuota,
    KiroUsageError,
    fetch_kiro_usage,
    parse_kiro_usage,
)


SAMPLE = """\
Estimated Usage | resets on 2026-12-01 | KIRO FREE
Credits (0.20 of 50 covered in plan), 0.4%
Manage your plan at https://app.kiro.dev/account/usage
"""


def test_parse_kiro_usage_sample() -> None:
    assert parse_kiro_usage(SAMPLE) == KiroQuota(
        plan="KIRO FREE",
        used=0.2,
        limit=50,
        used_percent=0.4,
        reset_time=datetime(2026, 12, 1, tzinfo=timezone.utc),
    )


def test_parse_kiro_usage_strips_ansi() -> None:
    colored = SAMPLE.replace("KIRO FREE", "\x1b[32mKIRO FREE\x1b[0m")
    assert parse_kiro_usage(colored).plan == "KIRO FREE"


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("Credits (0.20 of 50 covered in plan)\nEstimated Usage | KIRO FREE", "missing reset date"),
        ("Estimated Usage | resets on 2026-12-01 | KIRO FREE", "missing credits"),
        ("Credits (0.20 of 50 covered in plan)\nresets on 2026-12-01", "missing plan"),
        (
            "Estimated Usage | resets on 2026-12-01 | KIRO FREE\nCredits (0.20 of 0 covered in plan)",
            "invalid limit",
        ),
    ],
)
def test_parse_kiro_usage_missing_piece(text: str, message: str) -> None:
    with pytest.raises(KiroUsageError, match=message):
        parse_kiro_usage(text)


def test_fetch_kiro_usage_uses_default_command_and_options() -> None:
    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert Path(argv[0]).stem.casefold() == "kiro-cli"
        assert argv[1:] == ["chat", "--no-interactive", "/usage"]
        assert kwargs == {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "timeout": 60,
            "check": False,
            "shell": False,
            "encoding": "utf-8",
            "errors": "replace",
        }
        return subprocess.CompletedProcess(argv, 0, SAMPLE, "")

    assert fetch_kiro_usage(run=fake_run).used == 0.2


def test_fetch_nonzero_exit_uses_only_first_stderr_line() -> None:
    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "boom\nprivate detail")

    with pytest.raises(KiroUsageError, match="^boom$"):
        fetch_kiro_usage(run=fake_run)


def test_fetch_timeout_raises_kiro_usage_error() -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("kiro-cli", 60)

    with pytest.raises(KiroUsageError):
        fetch_kiro_usage(run=fake_run)
