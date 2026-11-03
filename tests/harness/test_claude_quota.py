from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from harness.claude_runner import ClaudeAgentRunner
from harness.config import HarnessConfig
from tests.portable import make_python_script


def _config(tmp_path: Path, command: str) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "execution": {"watchdog_poll_seconds": 0.01},
                "claude": {"enabled": True, "command": command},
            }
        ),
        encoding="utf-8",
    )
    return HarnessConfig.load(path)


def _runner_result(
    tmp_path: Path,
    message: str,
    *,
    exit_code: int = 1,
    is_error: bool = False,
):
    event = json.dumps(
        {
            "type": "result",
            "subtype": "error" if is_error else "success",
            "is_error": is_error,
            "result": message,
        }
    )
    fake = make_python_script(
        tmp_path / "fake-claude.py",
        (
            "import sys\n"
            "_ = sys.stdin.read()\n"
            f"print({event!r})\n"
            f"raise SystemExit({exit_code})\n"
        ),
    )
    runner = ClaudeAgentRunner(_config(tmp_path, str(fake)), tmp_path / "logs")
    return runner._invoke(
        task_id="CLAUDE-ERROR",
        phase="implement",
        prompt="test",
        workspace=tmp_path,
        model="sonnet",
        effort="medium",
        timeout_minutes=1,
        log_name="claude-error.log",
    )


@pytest.mark.parametrize(
    ("message", "expected_quota", "expected_cause", "expected_auth"),
    [
        ("You've hit your usage limit", True, None, False),
        ("Limit reached for this billing period", True, None, False),
        ('error type: "rate_limit_error"', True, None, False),
        ("HTTP 429 Too Many Requests", True, None, False),
        ("Network error while contacting api.anthropic.com", False, "network", False),
        ("Unable to connect to Anthropic services", False, "network", False),
        ("ECONNRESET during request", False, "network", False),
        ("API Error: overloaded_error", False, "provider_busy", False),
        ("HTTP 529: service overloaded", False, "provider_busy", False),
        ('error type: "api_error"', False, "provider_error", False),
        ("API Error: 500", False, "provider_error", False),
        ("HTTP 500 Internal Server Error", False, "provider_error", False),
        ("HTTP 503 Service Unavailable", False, "provider_error", False),
        ("Claude Code is not logged in; run /login", False, None, True),
        ("Authentication required", False, None, True),
    ],
)
def test_failed_claude_output_is_classified(
    tmp_path: Path,
    message: str,
    expected_quota: bool,
    expected_cause: str | None,
    expected_auth: bool,
):
    result = _runner_result(tmp_path, message)

    assert not result.ok
    assert result.quota_exhausted is expected_quota
    assert result.waiting_cause == expected_cause
    assert result.auth_failure is expected_auth
    assert result.capacity_exhausted is (expected_cause == "provider_busy")


def test_is_error_claude_result_is_classified(tmp_path: Path):
    result = _runner_result(
        tmp_path,
        'request failed with "api_error"',
        exit_code=0,
        is_error=True,
    )

    assert not result.ok
    assert result.waiting_cause == "provider_error"


@pytest.mark.parametrize("message", ["rate limit handling is complete", "overloaded UI fixed"])
def test_successful_claude_answer_is_not_pattern_matched(tmp_path: Path, message: str):
    result = _runner_result(tmp_path, message, exit_code=0)

    assert result.ok
    assert not result.quota_exhausted
    assert result.waiting_cause is None
    assert not result.auth_failure
