from __future__ import annotations

import os
from pathlib import Path

import yaml

from harness.codex_runner import CodexAgentRunner
from harness.config import HarnessConfig
from tests.portable import make_python_script


def _write_config(
    tmp_path: Path,
    codex_command: str,
    *,
    stall_timeout_minutes: float,
    watchdog_poll_seconds: float,
) -> HarnessConfig:
    payload = {
        "execution": {
            "max_parallel_agents": 1,
            "default_max_attempts": 2,
            "default_timeout_minutes": 1,
            "stall_timeout_minutes": stall_timeout_minutes,
            "watchdog_poll_seconds": watchdog_poll_seconds,
        },
        "cursor": {
            "enabled": True,
            "command": "agent",
            "models": {"worker": "cheap"},
        },
        "providers": {"priority": ["codex", "cursor"]},
        "codex": {
            "enabled": True,
            "command": codex_command,
            "reasoning_effort": "medium",
            "sandbox": "workspace-write",
            "implementation_sequence": [{"model": "test-model", "reasoning_effort": "medium"}],
            "review_model": "test-model",
            "planner_model": "test-model",
        },
        "retry": {"sequence": ["worker"]},
        "verification": {"global_task_commands": [], "stage_commands": []},
        "paths": {"protected": [], "review_required": []},
    }
    path = tmp_path / "harness.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return HarnessConfig.load(path)


def _git_init(workspace: Path) -> None:
    os.system(f"git -C {workspace} init -q")


def test_codex_watchdog_allows_silent_agent_with_file_activity(tmp_path: Path):
    """Silent Codex that keeps writing worktree files must not be stalled."""
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    _git_init(workspace)

    fake = make_python_script(
        tmp_path / "fake-codex-active.py",
        """from __future__ import annotations
import json
import sys
import time
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.0.0")
    raise SystemExit(0)
if args == ["login", "status"]:
    print("Logged in using ChatGPT")
    raise SystemExit(0)
if not args or args[0] != "exec":
    raise SystemExit(2)

# Stay silent on stdout/stderr while touching the worktree longer than the stall window.
workspace = Path(args[args.index("--cd") + 1])
for i in range(80):
    with (workspace / "progress.txt").open("a", encoding="utf-8") as fh:
        fh.write(str(i))
    time.sleep(0.15)
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "DONE"}}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}}))
""",
    )
    config = _write_config(
        tmp_path,
        str(fake),
        stall_timeout_minutes=0.15,
        watchdog_poll_seconds=0.05,
    )
    runner = CodexAgentRunner(config, tmp_path / "logs")
    result = runner._invoke(
        task_id="T-ACTIVE",
        phase="implement",
        prompt="work silently",
        workspace=workspace,
        model="test-model",
        timeout_minutes=1,
        log_name="codex-active.log",
    )
    assert result.ok
    assert not result.stalled
    assert (workspace / "progress.txt").read_text(encoding="utf-8") == "".join(
        str(i) for i in range(80)
    )


def test_codex_watchdog_stalls_silent_idle_agent(tmp_path: Path):
    """Silent Codex with no file activity must stall."""
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    _git_init(workspace)

    fake = make_python_script(
        tmp_path / "fake-codex-idle.py",
        """from __future__ import annotations
import sys
import time

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.0.0")
    raise SystemExit(0)
if args == ["login", "status"]:
    print("Logged in using ChatGPT")
    raise SystemExit(0)
if not args or args[0] != "exec":
    raise SystemExit(2)

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
    runner = CodexAgentRunner(config, tmp_path / "logs")
    result = runner._invoke(
        task_id="T-IDLE",
        phase="implement",
        prompt="do nothing",
        workspace=workspace,
        model="test-model",
        timeout_minutes=1,
        log_name="codex-idle.log",
    )
    assert not result.ok
    assert result.stalled
    assert result.waiting_cause == "timeout"
