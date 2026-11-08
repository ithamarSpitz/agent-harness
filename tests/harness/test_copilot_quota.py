from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.copilot_runner import CopilotAgentRunner
from tests.harness.test_copilot_runner import _write_config
from tests.portable import make_python_script


def _fake_failed_call(tmp_path: Path, text: str) -> Path:
    return make_python_script(
        tmp_path / "fake-copilot-failure.py",
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('copilot fake')\n"
        "else:\n"
        f"    print({text!r})\n"
        "    raise SystemExit(1)\n",
    )


def _invoke(runner: CopilotAgentRunner, tmp_path: Path):  # type: ignore[no-untyped-def]
    return runner._invoke(
        task_id="S14-T5",
        phase="review",
        prompt="test",
        workspace=tmp_path,
        model="auto",
        auto_tier="intelligence",
        effort=None,
        timeout_minutes=1,
        log_name="copilot-failure.log",
        read_only=True,
    )


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Premium request quota exceeded", "quota"),
        ("Request limit reached", "quota"),
        ("You are not logged in", "auth"),
        ('Model "gpt-5-mini" from --model flag is not available.', "model"),
        ("HTTP 429 rate limit", "provider_busy"),
        ("Connection refused by host", "network"),
        ("HTTP 503 service unavailable", "provider_error"),
    ],
)
def test_failed_call_is_classified(tmp_path: Path, text: str, kind: str) -> None:
    fake = _fake_failed_call(tmp_path, text)
    runner = CopilotAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    result = _invoke(runner, tmp_path)

    assert not result.ok
    assert result.quota_exhausted is (kind == "quota")
    assert result.auth_failure is (kind == "auth")
    assert result.model_unavailable is (kind == "model")
    assert result.capacity_exhausted is (kind == "provider_busy")
    if kind in {"quota", "auth", "model"}:
        assert result.waiting_cause is None
    else:
        assert result.waiting_cause == kind
    if kind == "model":
        assert result.error == "Copilot model unavailable: auto"


def test_timeout_is_classified(tmp_path: Path) -> None:
    fake = make_python_script(
        tmp_path / "fake-copilot-timeout.py",
        "import sys, time\n"
        "if '--version' in sys.argv:\n"
        "    print('copilot fake')\n"
        "else:\n"
        "    time.sleep(5)\n",
    )
    runner = CopilotAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    result = runner._invoke(
        task_id="TIMEOUT",
        phase="review",
        prompt="test",
        workspace=tmp_path,
        model="auto",
        auto_tier=None,
        effort=None,
        timeout_minutes=0.001,  # type: ignore[arg-type]
        log_name="timeout.log",
        read_only=True,
    )

    assert not result.ok
    assert result.timed_out
    assert result.waiting_cause == "timeout"
    assert result.transient_error


def test_stall_is_classified(tmp_path: Path) -> None:
    fake = make_python_script(
        tmp_path / "fake-copilot-stall.py",
        "import sys, time\n"
        "if '--version' in sys.argv:\n"
        "    print('copilot fake')\n"
        "else:\n"
        "    time.sleep(5)\n",
    )
    config = _write_config(tmp_path, str(fake))
    config.execution.stall_timeout_minutes = 0.001
    config.execution.watchdog_poll_seconds = 0.01
    runner = CopilotAgentRunner(config, tmp_path / "logs")

    result = _invoke(runner, tmp_path)

    assert not result.ok
    assert result.stalled
    assert result.waiting_cause == "timeout"
    assert result.transient_error


def test_successful_answer_containing_quota_is_not_a_failure(tmp_path: Path) -> None:
    answer = "The quota documentation is correct."
    stream = "\n".join(
        (
            json.dumps({"type": "assistant.message", "data": {"content": answer}}),
            json.dumps({"type": "result", "exitCode": 0, "usage": {}}),
        )
    )
    fake = make_python_script(
        tmp_path / "fake-copilot-success.py",
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('copilot fake')\n"
        "else:\n"
        f"    print({stream!r})\n",
    )
    runner = CopilotAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    result = _invoke(runner, tmp_path)

    assert result.ok
    assert result.output == answer
    assert not result.quota_exhausted
    assert result.waiting_cause is None
