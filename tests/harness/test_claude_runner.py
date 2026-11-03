from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
import yaml

from harness.claude_runner import ClaudeAgentRunner
from harness.config import HarnessConfig
from harness.usage import UsageRecorder
from tests.portable import make_python_script


FIXTURE = Path(__file__).parent / "fixtures" / "claude-stream-sample.jsonl"


def _write_config(
    tmp_path: Path,
    claude_command: str = "claude",
    *,
    stall_timeout_minutes: float = 12.0,
    watchdog_poll_seconds: float = 10.0,
) -> HarnessConfig:
    payload = {
        "execution": {
            "max_parallel_agents": 1,
            "default_max_attempts": 2,
            "default_timeout_minutes": 1,
            "stall_timeout_minutes": stall_timeout_minutes,
            "watchdog_poll_seconds": watchdog_poll_seconds,
        },
        "worktree": {},
        "code_map": {"command": "graphify"},
        "cursor": {"models": {"worker": "test-model"}},
        "claude": {
            "enabled": True,
            "command": claude_command,
            "implementation_sequence": [
                {"model": "sonnet", "effort": "medium"},
                {"model": "sonnet", "effort": "high"},
                {"model": "opus", "effort": "high"},
            ],
            "review_model": "sonnet",
            "review_effort": "high",
            "escalation_model": "opus",
            "escalation_effort": "high",
            "permission_mode": "bypassPermissions",
        },
        "verification": {"global_task_commands": [], "stage_commands": []},
        "paths": {"protected": [], "review_required": []},
    }
    path = tmp_path / "harness.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return HarnessConfig.load(path)


def _git_init(workspace: Path) -> None:
    os.system(f"git -C {workspace} init -q")


def _expected_command(command: str, model: str, effort: str) -> list[str]:
    return [
        command,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        model,
        "--effort",
        effort,
        "--permission-mode",
        "bypassPermissions",
    ]


@pytest.mark.parametrize(
    ("attempt", "model", "effort"),
    [
        (1, "sonnet", "medium"),
        (2, "sonnet", "high"),
        (3, "opus", "high"),
        (4, "opus", "high"),
    ],
)
def test_claude_command_line_for_implementation_attempts(
    tmp_path: Path, attempt: int, model: str, effort: str,
):
    runner = ClaudeAgentRunner(_write_config(tmp_path), tmp_path / "logs")
    resolved_model, resolved_effort = runner._implementation_profile(attempt)
    assert (resolved_model, resolved_effort) == (model, effort)
    assert runner._command(resolved_model, resolved_effort) == _expected_command(
        "claude", model, effort
    )


def test_claude_command_line_for_review_and_escalation(tmp_path: Path):
    runner = ClaudeAgentRunner(_write_config(tmp_path), tmp_path / "logs")

    review_model, review_effort = runner._review_profile()
    assert (review_model, review_effort) == ("sonnet", "high")
    assert runner._command(review_model, review_effort) == _expected_command(
        "claude", "sonnet", "high"
    )

    esc_model, esc_effort = runner._implementation_profile(1, model_class="escalation")
    assert (esc_model, esc_effort) == ("opus", "high")
    assert runner._command(esc_model, esc_effort) == _expected_command(
        "claude", "opus", "high"
    )


def test_claude_stream_fixture_extracts_answer_usage_and_ok(tmp_path: Path):
    runner = ClaudeAgentRunner(_write_config(tmp_path), tmp_path / "logs")
    raw = FIXTURE.read_text(encoding="utf-8")

    answer, diagnostics, usage, tool_calls, code_map_calls, reported_error = runner._parse_output(
        raw, "graphify"
    )

    assert answer == "ok"
    assert diagnostics == ""
    assert usage == {
        "input_tokens": 2,
        "cached_input_tokens": 14457,
        "cache_write_tokens": 10347,
        "output_tokens": 4,
    }
    assert tool_calls == 0
    assert code_map_calls == 0
    assert not reported_error


def test_claude_counts_bash_tool_and_code_map_calls(tmp_path: Path):
    runner = ClaudeAgentRunner(_write_config(tmp_path), tmp_path / "logs")
    stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Bash",
                                "input": {"command": "graphify query 'x' --budget 500"},
                            },
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"file_path": "harness/claude_runner.py"},
                            },
                        ]
                    },
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "DONE",
                    "usage": {
                        "input_tokens": 1,
                        "output_tokens": 1,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                    },
                }
            ),
        ]
    )

    answer, _, _, tool_calls, code_map_calls, reported_error = runner._parse_output(
        stream, "graphify"
    )

    assert answer == "DONE"
    assert tool_calls == 2
    assert code_map_calls == 1
    assert not reported_error


def test_claude_is_error_result_fails_with_text(tmp_path: Path):
    stream = json.dumps(
        {
            "type": "result",
            "subtype": "error",
            "is_error": True,
            "result": "permission denied writing file",
            "usage": {
                "input_tokens": 3,
                "output_tokens": 2,
                "cache_read_input_tokens": 1,
                "cache_creation_input_tokens": 0,
            },
        }
    )
    fake = make_python_script(
        tmp_path / "fake-claude-error.py",
        f"import sys\n_ = sys.stdin.read()\nsys.stdout.write({stream!r})\n",
    )
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    runner = ClaudeAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs", usage=usage)

    result = runner._invoke(
        task_id="ERR",
        phase="implement",
        prompt="x",
        workspace=tmp_path,
        model="sonnet",
        effort="medium",
        timeout_minutes=1,
        log_name="error.log",
        attempt=1,
    )

    assert not result.ok
    assert result.output == "permission denied writing file"
    assert result.error == "Claude reported an error"
    assert result.provider == "claude"
    assert result.waiting_cause is None
    event = usage.events()[0]
    assert event["ok"] is False
    assert event["input_tokens"] == 3
    assert event["output_tokens"] == 2
    assert event["cached_input_tokens"] == 1
    assert event["cache_write_tokens"] is None
    assert event["effort"] == "medium"


def test_claude_watchdog_stalls_silent_idle_agent(tmp_path: Path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    _git_init(workspace)

    fake = make_python_script(
        tmp_path / "fake-claude-idle.py",
        """from __future__ import annotations
import sys
import time

_ = sys.stdin.read()
time.sleep(5)
""",
    )
    config = _write_config(
        tmp_path,
        str(fake),
        stall_timeout_minutes=0.01,
        watchdog_poll_seconds=0.02,
    )
    runner = ClaudeAgentRunner(config, tmp_path / "logs")
    result = runner._invoke(
        task_id="T-IDLE",
        phase="implement",
        prompt="do nothing",
        workspace=workspace,
        model="sonnet",
        effort="medium",
        timeout_minutes=1,
        log_name="claude-idle.log",
    )
    assert not result.ok
    assert result.stalled
    assert result.waiting_cause == "timeout"


def test_claude_watchdog_allows_silent_agent_with_file_activity(tmp_path: Path):
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    _git_init(workspace)

    fake = make_python_script(
        tmp_path / "fake-claude-active.py",
        """from __future__ import annotations
import json
import sys
import time
from pathlib import Path

_ = sys.stdin.read()
workspace = Path.cwd()
for i in range(80):
    with (workspace / "progress.txt").open("a", encoding="utf-8") as fh:
        fh.write(str(i))
    time.sleep(0.15)
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "DONE",
    "usage": {
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    },
}))
""",
    )
    config = _write_config(
        tmp_path,
        str(fake),
        stall_timeout_minutes=0.15,
        watchdog_poll_seconds=0.05,
    )
    runner = ClaudeAgentRunner(config, tmp_path / "logs")
    result = runner._invoke(
        task_id="T-ACTIVE",
        phase="implement",
        prompt="work silently",
        workspace=workspace,
        model="sonnet",
        effort="medium",
        timeout_minutes=1,
        log_name="claude-active.log",
    )
    assert result.ok
    assert not result.stalled
    assert result.output == "DONE"
    assert (workspace / "progress.txt").read_text(encoding="utf-8") == "".join(
        str(i) for i in range(80)
    )


def test_claude_prompt_is_sent_on_stdin(tmp_path: Path):
    fake = make_python_script(
        tmp_path / "fake-claude-stdin.py",
        """from __future__ import annotations
import json
import sys

prompt = sys.stdin.read()
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": f"heard:{prompt.strip()}",
    "usage": {
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    },
}))
""",
    )
    runner = ClaudeAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")
    result = runner._invoke(
        task_id="STDIN",
        phase="implement",
        prompt="hello-from-stdin",
        workspace=tmp_path,
        model="sonnet",
        effort="medium",
        timeout_minutes=1,
        log_name="stdin.log",
    )
    assert result.ok
    assert result.output == "heard:hello-from-stdin"
