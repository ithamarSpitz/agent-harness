from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from harness.config import HarnessConfig
from harness.copilot_runner import CopilotAgentRunner
from harness.models import TaskSpec
from harness.usage import UsageRecorder
from tests.portable import make_python_script


FIXTURE = Path(__file__).parent / "fixtures" / "copilot-stream-sample.jsonl"
IMPLEMENT_DENIES = [
    "shell(git push)",
    "shell(git commit)",
    "shell(git reset)",
    "shell(git checkout)",
    "shell(git rebase)",
    "shell(gh)",
]
READ_ALLOWS = [
    "shell(git diff:*)",
    "shell(git log:*)",
    "shell(git show:*)",
    "shell(git status:*)",
]


def _write_config(tmp_path: Path, command: str = "copilot") -> HarnessConfig:
    payload = {
        "execution": {
            "max_parallel_agents": 1,
            "default_max_attempts": 2,
            "default_timeout_minutes": 1,
            "stall_timeout_minutes": 1,
            "watchdog_poll_seconds": 0.1,
        },
        "worktree": {},
        "code_map": {"command": "graphify"},
        "cursor": {"models": {"worker": "test-model"}},
        "copilot": {
            "enabled": True,
            "command": command,
            "implementation_sequence": [
                {"model": "auto", "auto_tier": "efficiency"},
                {
                    "model": "auto",
                    "auto_tier": "balance",
                    "effort": "medium",
                },
                {"model": "gpt-5.6-luna", "effort": "high"},
            ],
            "review_model": "auto",
            "review_auto_tier": "intelligence",
            "review_effort": "low",
            "escalation_model": "auto",
            "escalation_auto_tier": "intelligence",
            "escalation_effort": "max",
        },
        "verification": {"global_task_commands": [], "stage_commands": []},
        "paths": {"protected": [], "review_required": []},
    }
    path = tmp_path / "harness.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return HarnessConfig.load(path)


def _task(tmp_path: Path) -> TaskSpec:
    brief = tmp_path / "brief.md"
    brief.write_text("# Brief\n", encoding="utf-8")
    return TaskSpec(
        id="S14-T4",
        file=brief,
        sprint="S14",
        stage=2,
        model_class="hard_worker",
        verification=["python -m pytest -q tests/harness"],
    )


def _fake_copilot(tmp_path: Path) -> Path:
    stream = FIXTURE.read_text(encoding="utf-8")
    return make_python_script(
        tmp_path / "fake-copilot.py",
        "import json, os, pathlib, sys\n"
        "capture = os.environ.get('COPILOT_TEST_CAPTURE')\n"
        "if capture:\n"
        "    pathlib.Path(capture).write_text(json.dumps({\n"
        "        'argv': sys.argv[1:],\n"
        "        'tier': os.environ.get('COPILOT_AUTO_TIER'),\n"
        "    }), encoding='utf-8')\n"
        "if '--version' in sys.argv:\n"
        "    print('copilot fake-1.0')\n"
        "else:\n"
        f"    print({stream!r})\n",
    )


def _pairs(flag: str, values: list[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        result += [flag, value]
    return result


def test_implement_exact_argv_and_child_only_auto_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_copilot(tmp_path)
    capture = tmp_path / "implement.json"
    runner = CopilotAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")
    task = _task(tmp_path)
    child_env = dict(os.environ)
    child_env["COPILOT_TEST_CAPTURE"] = str(capture)
    monkeypatch.delenv("COPILOT_AUTO_TIER", raising=False)

    result = runner.implement(task, tmp_path, 1, 1, None, env=child_env)

    assert result.ok
    recorded = json.loads(capture.read_text(encoding="utf-8"))
    prompt = recorded["argv"][1]
    assert recorded["argv"] == [
        "-p",
        prompt,
        "--output-format",
        "json",
        "-C",
        str(tmp_path),
        "--no-auto-update",
        "--disable-builtin-mcps",
        "--model",
        "auto",
        "--allow-all-tools",
        *_pairs("--deny-tool", IMPLEMENT_DENIES),
    ]
    assert prompt.startswith("You are executing task S14-T4.")
    assert recorded["tier"] == "efficiency"
    assert "COPILOT_AUTO_TIER" not in os.environ


def test_review_exact_argv_and_read_only_rules(tmp_path: Path) -> None:
    fake = _fake_copilot(tmp_path)
    capture = tmp_path / "review.json"
    runner = CopilotAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")
    child_env = dict(os.environ)
    child_env["COPILOT_TEST_CAPTURE"] = str(capture)

    result = runner.review(
        _task(tmp_path), tmp_path, "base-sha", "tests pass", 1, env=child_env
    )

    assert result.ok
    recorded = json.loads(capture.read_text(encoding="utf-8"))
    prompt = recorded["argv"][1]
    assert recorded["argv"] == [
        "-p",
        prompt,
        "--output-format",
        "json",
        "-C",
        str(tmp_path),
        "--no-auto-update",
        "--disable-builtin-mcps",
        "--model",
        "auto",
        "--reasoning-effort",
        "low",
        "--deny-tool",
        "write",
        *_pairs("--allow-tool", READ_ALLOWS),
    ]
    assert prompt.startswith("Review task S14-T4 as an independent reviewer.")
    assert recorded["tier"] == "intelligence"


def test_parse_fixture_and_usage_event(tmp_path: Path) -> None:
    answer, model, premium, success = CopilotAgentRunner._parse_output(
        FIXTURE.read_text(encoding="utf-8")
    )
    assert (answer, model, premium, success) == (
        "fixture answer",
        "gpt-5.6-luna",
        1,
        True,
    )

    fake = _fake_copilot(tmp_path)
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    runner = CopilotAgentRunner(
        _write_config(tmp_path, str(fake)), tmp_path / "logs", usage=usage
    )
    result = runner.implement(_task(tmp_path), tmp_path, 1, 1, None)
    event = usage.events()[0]
    assert result.model == "gpt-5.6-luna"
    assert event["provider"] == "copilot"
    assert event["model"] == "gpt-5.6-luna"
    assert event["requested_model"] == "auto"
    assert event["auto_tier"] == "efficiency"
    assert event["premium_requests"] == 1
    assert event["input_tokens"] == 0
    assert event["output_tokens"] == 0


def test_stream_without_result_event_fails(tmp_path: Path) -> None:
    stream = json.dumps(
        {"type": "assistant.message", "data": {"content": "unfinished"}}
    )
    fake = make_python_script(
        tmp_path / "no-result.py",
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('copilot fake-1.0')\n"
        "else:\n"
        f"    print({stream!r})\n",
    )
    runner = CopilotAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    result = runner.implement(_task(tmp_path), tmp_path, 1, 1, None)

    assert not result.ok
    assert result.output == "unfinished"
    assert result.error == "Copilot JSON stream contained no successful result event"


def test_attempt_sequence_uses_second_and_then_last_entry(tmp_path: Path) -> None:
    runner = CopilotAgentRunner(_write_config(tmp_path), tmp_path / "logs")

    assert runner._implementation_profile(2) == ("auto", "balance", "medium")
    assert runner._implementation_profile(5) == ("gpt-5.6-luna", None, "high")
    assert runner._implementation_profile(1, model_class="escalation") == (
        "auto",
        "intelligence",
        "max",
    )


def test_profile_without_auto_tier_clears_inherited_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_copilot(tmp_path)
    capture = tmp_path / "no-tier.json"
    runner = CopilotAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")
    monkeypatch.setenv("COPILOT_AUTO_TIER", "intelligence")
    monkeypatch.setenv("COPILOT_TEST_CAPTURE", str(capture))

    result = runner.implement(_task(tmp_path), tmp_path, 1, 5, None)

    assert result.ok
    recorded = json.loads(capture.read_text(encoding="utf-8"))
    assert recorded["tier"] is None
    assert os.environ["COPILOT_AUTO_TIER"] == "intelligence"


def test_long_prompt_uses_file_and_add_dir(tmp_path: Path) -> None:
    fake = _fake_copilot(tmp_path)
    capture = tmp_path / "long.json"
    runner = CopilotAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")
    child_env = dict(os.environ)
    child_env["COPILOT_TEST_CAPTURE"] = str(capture)
    prompt = "x" * 24_001

    result = runner._invoke(
        task_id="LONG",
        phase="implement",
        prompt=prompt,
        workspace=tmp_path,
        model="auto",
        auto_tier="balance",
        effort=None,
        timeout_minutes=1,
        log_name="long.log",
        read_only=False,
        env=child_env,
    )

    assert result.ok
    recorded = json.loads(capture.read_text(encoding="utf-8"))
    short_prompt = recorded["argv"][1]
    assert len(short_prompt) < 24_000
    assert recorded["argv"][-2:] == ["--deny-tool", "shell(gh)"]
    add_dir_index = recorded["argv"].index("--add-dir")
    assert recorded["argv"][add_dir_index + 1] == str(tmp_path / "logs")
    prompt_path = tmp_path / "logs" / "long-prompt.md"
    assert prompt_path.read_text(encoding="utf-8") == prompt
    assert str(prompt_path) in short_prompt


def test_availability_is_cached_and_checks_only_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _fake_copilot(tmp_path)
    runner = CopilotAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    assert runner.availability() == (True, "copilot fake-1.0")
    monkeypatch.setattr("harness.copilot_runner.subprocess.run", lambda *a, **k: None)
    assert runner.availability() == (True, "copilot fake-1.0")
