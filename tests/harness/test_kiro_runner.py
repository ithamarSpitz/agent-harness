from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from harness.config import HarnessConfig
from harness.kiro_runner import KIRO_NOT_FOUND, KiroAgentRunner
from harness.models import TaskSpec
from harness.usage import UsageRecorder
from tests.portable import make_python_script


def _write_config(tmp_path: Path, command: str = "kiro-cli") -> HarnessConfig:
    payload = {
        "execution": {
            "max_parallel_agents": 1,
            "default_max_attempts": 3,
            "default_timeout_minutes": 1,
            "stall_timeout_minutes": 1,
            "watchdog_poll_seconds": 0.1,
        },
        "worktree": {},
        "code_map": {"command": "graphify"},
        "cursor": {"models": {"worker": "test-model"}},
        "kiro": {
            "enabled": True,
            "command": command,
            "implementation_sequence": [
                "glm-5",
                "claude-sonnet-4.5",
                "claude-sonnet-4.5",
            ],
            "review_model": "claude-haiku-4.5",
            "escalation_model": "claude-sonnet-4.5",
        },
        "verification": {"global_task_commands": [], "stage_commands": []},
        "paths": {"protected": [], "review_required": []},
    }
    path = tmp_path / "harness.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return HarnessConfig.load(path)


def _task(tmp_path: Path, *, model_class: str = "hard_worker") -> TaskSpec:
    brief = tmp_path / "brief.md"
    brief.write_text("# Brief\n", encoding="utf-8")
    return TaskSpec(
        id="S17-T4",
        file=brief,
        sprint="S17",
        stage=2,
        model_class=model_class,
        verification=["python -m pytest -q tests/harness"],
    )


def _success_stream(text: str, *, credits: float = 1.0) -> str:
    return "\n".join(
        [
            json.dumps(
                {
                    "type": "metadata",
                    "data": {
                        "meteringUsage": [
                            {"unit": "credit", "value": credits},
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "runFinished",
                    "data": {
                        "status": "success",
                        "stopReason": "end_turn",
                        "finalText": text,
                    },
                }
            ),
        ]
    )


def _fake_kiro(tmp_path: Path, stream: str) -> Path:
    return make_python_script(
        tmp_path / "fake-kiro.py",
        "import json, os, pathlib, sys\n"
        "capture = os.environ.get('KIRO_TEST_CAPTURE')\n"
        "prompt = sys.stdin.read()\n"
        "if capture:\n"
        "    pathlib.Path(capture).write_text(json.dumps({\n"
        "        'argv': sys.argv[1:], 'prompt': prompt,\n"
        "    }), encoding='utf-8')\n"
        "if '--version' in sys.argv:\n"
        "    print('kiro-cli 3.1.0')\n"
        "else:\n"
        f"    print({stream!r})\n",
    )


def _capturing_env(capture: Path) -> dict[str, str]:
    env = dict(os.environ)
    env["KIRO_TEST_CAPTURE"] = str(capture)
    return env


def test_implement_exact_command_and_prompt_on_stdin(tmp_path: Path) -> None:
    fake = _fake_kiro(tmp_path, _success_stream("STATUS: done"))
    capture = tmp_path / "implement.json"
    runner = KiroAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    result = runner.implement(
        _task(tmp_path), tmp_path, 1, 1, None, env=_capturing_env(capture)
    )

    assert result.ok
    recorded = json.loads(capture.read_text(encoding="utf-8"))
    assert recorded["argv"] == [
        "chat",
        "--no-interactive",
        "--output-format",
        "stream-json",
        "--model",
        "glm-5",
        "--trust-workspace",
        "--trust-all-tools",
    ]
    assert recorded["prompt"].startswith("You are executing task S17-T4.")
    assert recorded["prompt"] not in recorded["argv"]


def test_review_exact_command_and_prompt_on_stdin(tmp_path: Path) -> None:
    fake = _fake_kiro(tmp_path, _success_stream("VERDICT: PASS"))
    capture = tmp_path / "review.json"
    runner = KiroAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    result = runner.review(
        _task(tmp_path),
        tmp_path,
        "base-sha",
        "tests pass",
        1,
        env=_capturing_env(capture),
    )

    assert result.ok
    recorded = json.loads(capture.read_text(encoding="utf-8"))
    assert recorded["argv"] == [
        "chat",
        "--no-interactive",
        "--output-format",
        "stream-json",
        "--model",
        "claude-haiku-4.5",
        "--trust-workspace",
        "--trust-tools=fs_read,execute_bash",
    ]
    assert recorded["prompt"].startswith(
        "Review task S17-T4 as an independent reviewer."
    )
    assert recorded["prompt"] not in recorded["argv"]


@pytest.mark.parametrize(
    ("attempt", "expected"),
    [(1, "glm-5"), (2, "claude-sonnet-4.5"), (3, "claude-sonnet-4.5")],
)
def test_implementation_sequence_models(
    tmp_path: Path, attempt: int, expected: str
) -> None:
    fake = _fake_kiro(tmp_path, _success_stream("STATUS: done"))
    capture = tmp_path / f"attempt-{attempt}.json"
    runner = KiroAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    result = runner.implement(
        _task(tmp_path),
        tmp_path,
        1,
        attempt,
        None,
        env=_capturing_env(capture),
    )

    assert result.ok
    argv = json.loads(capture.read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("--model") + 1] == expected


def test_escalation_models_for_implement_and_plan_change(tmp_path: Path) -> None:
    fake = _fake_kiro(tmp_path, _success_stream("STATUS: done"))
    capture = tmp_path / "escalation.json"
    runner = KiroAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")
    task = _task(tmp_path)

    result = runner.implement(
        task,
        tmp_path,
        1,
        1,
        None,
        model_class_override="escalation",
        env=_capturing_env(capture),
    )
    assert result.ok
    argv = json.loads(capture.read_text(encoding="utf-8"))["argv"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-4.5"

    plan_result = runner.plan_change(
        task,
        tmp_path,
        "failure",
        1,
        model_class="escalation",
        env=_capturing_env(capture),
    )
    assert plan_result.ok
    recorded = json.loads(capture.read_text(encoding="utf-8"))
    assert recorded["argv"][recorded["argv"].index("--model") + 1] == (
        "claude-sonnet-4.5"
    )
    assert recorded["prompt"].startswith(
        "You are acting as a read-only plan-repair analyst for task S17-T4."
    )


def test_success_result_and_usage_credits(tmp_path: Path) -> None:
    fake = _fake_kiro(tmp_path, _success_stream("STATUS: done\nChanged files", credits=1.5))
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    runner = KiroAgentRunner(
        _write_config(tmp_path, str(fake)), tmp_path / "logs", usage=usage
    )

    result = runner.implement(_task(tmp_path), tmp_path, 1, 1, None)

    assert result.ok
    assert result.output == "STATUS: done\nChanged files"
    assert result.provider == "kiro"
    assert result.model == "glm-5"
    event = usage.events()[0]
    assert event["provider"] == "kiro"
    assert event["model_class"] == "kiro"
    assert event["credits"] == 1.5
    assert "auto_tier" not in event
    assert "effort" not in event


def test_success_without_status_is_launch_failure_and_logs_tail(tmp_path: Path) -> None:
    stream = (
        "[denied] tool permission approval is not supported in non-interactive mode.\n"
        "[denied] tool permission approval is not supported in non-interactive mode.\n"
        + _success_stream("I changed it\nNo report")
    )
    fake = _fake_kiro(tmp_path, stream)
    runner = KiroAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    result = runner.implement(_task(tmp_path), tmp_path, 1, 1, None)

    assert not result.ok
    assert result.launch_failure
    assert result.error == "kiro finished without a STATUS report (2 denied tool calls)"
    log = (tmp_path / "logs" / "S17-T4-attempt-1-kiro.log").read_text(
        encoding="utf-8"
    )
    assert "kiro response tail:\nI changed it\nNo report" in log


@pytest.mark.parametrize(
    ("message", "attribute"),
    [
        ("The model 'missing-model' is not available", "model_unavailable"),
        ("You are out of credits for this month", "quota_exhausted"),
    ],
)
def test_fault_classification(tmp_path: Path, message: str, attribute: str) -> None:
    stream = "\n".join(
        [
            json.dumps({"type": "runError", "data": {"message": message}}),
            json.dumps(
                {
                    "type": "runFinished",
                    "data": {"status": "failed", "stopReason": "error"},
                }
            ),
        ]
    )
    fake = _fake_kiro(tmp_path, stream)
    runner = KiroAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    result = runner.implement(_task(tmp_path), tmp_path, 1, 1, None)

    assert not result.ok
    assert getattr(result, attribute)


def test_timeout_ignores_quota_like_output(tmp_path: Path) -> None:
    fake = make_python_script(
        tmp_path / "fake-timeout.py",
        "import time\n"
        "print('You are out of credits for this month', flush=True)\n"
        "time.sleep(10)\n",
    )
    runner = KiroAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    result = runner.implement(_task(tmp_path), tmp_path, 0, 1, None)

    assert not result.ok
    assert result.timed_out
    assert result.waiting_cause == "timeout"
    assert result.error == "Kiro timed out after 0 minutes"
    assert not result.quota_exhausted
    assert result.quota_scope is None


def test_command_not_found_does_not_start_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = KiroAgentRunner(_write_config(tmp_path), tmp_path / "logs")
    popen_called = False

    def fail_popen(*args: object, **kwargs: object) -> None:
        nonlocal popen_called
        popen_called = True
        raise AssertionError("Popen must not be called")

    monkeypatch.setattr("harness.kiro_runner.locate", lambda _command: None)
    monkeypatch.setattr("harness.kiro_runner.subprocess.Popen", fail_popen)

    result = runner.implement(_task(tmp_path), tmp_path, 1, 1, None)

    assert not result.ok
    assert result.launch_failure
    assert result.error == f"kiro could not start: {KIRO_NOT_FOUND}"
    assert not popen_called


def test_brief_check_uses_review_model_and_read_only_tools(tmp_path: Path) -> None:
    fake = _fake_kiro(tmp_path, _success_stream("Brief is coherent"))
    capture = tmp_path / "brief-check.json"
    runner = KiroAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    result = runner.brief_check(
        _task(tmp_path),
        tmp_path,
        "# Embedded brief",
        1,
        env=_capturing_env(capture),
    )

    assert result.ok
    recorded = json.loads(capture.read_text(encoding="utf-8"))
    assert recorded["argv"][-1] == "--trust-tools=fs_read,execute_bash"
    assert recorded["argv"][recorded["argv"].index("--model") + 1] == (
        "claude-haiku-4.5"
    )
    assert "# Embedded brief" in recorded["prompt"]
