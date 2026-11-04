from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from harness.agy_usage import AgyBucket, AgyUsageError, fetch_agy_usage, parse_agy_usage


FIXTURE = Path(__file__).parent / "fixtures" / "agy-usage-sample.json"


def test_recorded_usage_fixture_has_two_weekly_buckets() -> None:
    buckets = parse_agy_usage(FIXTURE.read_text(encoding="utf-8"))

    assert buckets == [
        AgyBucket(
            "gemini-weekly",
            "Gemini Models",
            "weekly",
            0.9796944260597229,
            datetime(2026, 11, 10, 13, 46, 45, tzinfo=timezone.utc),
        ),
        AgyBucket(
            "3p-weekly",
            "Claude and GPT models",
            "weekly",
            1.0,
            datetime(2026, 11, 11, 17, 58, 25, tzinfo=timezone.utc),
        ),
    ]


@pytest.mark.parametrize(
    ("payload", "expected_ids"),
    [
        ({"command": {"data": {}}}, []),
        (
            {
                "command": {
                    "data": {
                        "groups": [
                            {
                                "name": "mixed",
                                "buckets": [
                                    {
                                        "id": "missing-remaining",
                                        "window": "weekly",
                                        "reset_time": "2026-11-10T13:46:45Z",
                                    },
                                    {
                                        "id": "valid",
                                        "window": "weekly",
                                        "remaining_fraction": 0.5,
                                        "reset_time": "2026-11-10T15:46:45+02:00",
                                    },
                                ],
                            }
                        ]
                    }
                }
            },
            ["valid"],
        ),
    ],
)
def test_malformed_entries_are_skipped(payload: dict[str, Any], expected_ids: list[str]) -> None:
    assert [bucket.pool_id for bucket in parse_agy_usage(json.dumps(payload))] == expected_ids


@pytest.mark.parametrize("text", ["not json", "[]", "null", "{\"command\": null}"])
def test_non_usage_json_returns_no_buckets(text: str) -> None:
    assert parse_agy_usage(text) == []


def test_fetch_runs_in_empty_temporary_directory_with_closed_stdin() -> None:
    fixture_text = FIXTURE.read_text(encoding="utf-8")
    observed_cwd: Path | None = None

    def fake_run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal observed_cwd
        observed_cwd = Path(kwargs["cwd"])
        assert observed_cwd.is_dir()
        assert list(observed_cwd.iterdir()) == []
        assert argv == ["fake-agy", "-p", "/usage", "--output-format", "json"]
        assert kwargs == {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "cwd": str(observed_cwd),
            "timeout": 7,
            "check": False,
        }
        return subprocess.CompletedProcess(argv, 0, fixture_text, "")

    buckets = fetch_agy_usage(command="fake-agy", timeout=7, run=fake_run)

    assert [bucket.pool_id for bucket in buckets] == ["gemini-weekly", "3p-weekly"]
    assert observed_cwd is not None
    assert not observed_cwd.exists()


def test_fetch_timeout_raises_small_error_without_output() -> None:
    private_output = "private provider response"

    def fake_run(*_args: Any, **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired("agy", 3, output=private_output, stderr=private_output)

    with pytest.raises(AgyUsageError, match="timed out after 3 seconds") as caught:
        fetch_agy_usage(timeout=3, run=fake_run)

    assert private_output not in str(caught.value)


def test_fetch_nonzero_exit_raises_small_error_without_output() -> None:
    private_output = "private provider response"

    def fake_run(argv: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 9, private_output, private_output)

    with pytest.raises(AgyUsageError, match="exited with status 9") as caught:
        fetch_agy_usage(run=fake_run)

    assert private_output not in str(caught.value)
