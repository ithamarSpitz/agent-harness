from __future__ import annotations

import json
from pathlib import Path

import yaml

from harness.claude_runner import ClaudeAgentRunner
from harness.config import HarnessConfig
from harness.models import TaskSpec
from tests.portable import make_python_script


def _write_config(
    tmp_path: Path,
    claude_command: str,
    *,
    ui_review: dict | None = None,
) -> HarnessConfig:
    payload: dict = {
        "execution": {
            "max_parallel_agents": 1,
            "default_max_attempts": 2,
            "default_timeout_minutes": 1,
            "stall_timeout_minutes": 12.0,
            "watchdog_poll_seconds": 0.05,
        },
        "worktree": {},
        "code_map": {"command": "graphify"},
        "cursor": {"models": {"worker": "test-model"}},
        "claude": {
            "enabled": True,
            "command": claude_command,
            "implementation_sequence": [
                {"model": "sonnet", "effort": "medium"},
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
    if ui_review is not None:
        payload["ui_review"] = ui_review
    path = tmp_path / "harness.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return HarnessConfig.load(path)


def _task(tmp_path: Path) -> TaskSpec:
    task_file = tmp_path / "task.md"
    task_file.write_text("# task\n", encoding="utf-8")
    return TaskSpec(
        id="T1",
        file=task_file,
        sprint="S1",
        stage=1,
        review=True,
        allowed_paths=["**"],
    )


def _fake_claude_cli(tmp_path: Path) -> Path:
    return make_python_script(
        tmp_path / "fake-claude.py",
        """from __future__ import annotations
import json
import sys
from pathlib import Path

prompt = sys.stdin.read()
args = sys.argv[1:]
mcp_config = None
if "--mcp-config" in args:
    idx = args.index("--mcp-config")
    mcp_path = Path(args[idx + 1])
    mcp_config = {
        "path": str(mcp_path),
        "exists_during_call": mcp_path.is_file(),
        "payload": json.loads(mcp_path.read_text(encoding="utf-8")) if mcp_path.is_file() else None,
    }
workspace = Path.cwd()
(workspace / "fake-claude-argv.json").write_text(json.dumps(args), encoding="utf-8")
(workspace / "fake-claude-mcp.json").write_text(json.dumps(mcp_config), encoding="utf-8")
(workspace / "fake-claude-stdin.txt").write_text(prompt, encoding="utf-8")
print(json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "ok",
    "usage": {
        "input_tokens": 1,
        "output_tokens": 1,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    },
}))
""",
    )


def _assert_mcp_flags(argv: list[str], expected_path: Path) -> None:
    assert "--mcp-config" in argv
    assert argv[argv.index("--mcp-config") + 1] == str(expected_path)
    assert "--strict-mcp-config" in argv


def test_claude_implement_uses_playwright_mcp_when_ui_review_enabled(tmp_path: Path) -> None:
    fake = _fake_claude_cli(tmp_path)
    package = "@playwright/mcp@0.0.84"
    config = _write_config(
        tmp_path,
        str(fake),
        ui_review={
            "enabled": True,
            "start_commands": ["npm start"],
            "url": "http://localhost:3000",
            "playwright_mcp_package": package,
        },
    )
    log_dir = tmp_path / "logs"
    runner = ClaudeAgentRunner(config, log_dir)
    workspace = tmp_path / "wt"
    workspace.mkdir()
    mcp_path = log_dir / "T1-implement-claude-mcp.json"

    result = runner.implement(_task(tmp_path), workspace, timeout_minutes=1, attempt=1, previous_failure=None)

    assert result.ok
    argv = json.loads((workspace / "fake-claude-argv.json").read_text(encoding="utf-8"))
    _assert_mcp_flags(argv, mcp_path)
    during = json.loads((workspace / "fake-claude-mcp.json").read_text(encoding="utf-8"))
    assert during["exists_during_call"] is True
    assert during["payload"] == {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": ["-y", package],
            }
        }
    }
    assert list(during["payload"]["mcpServers"]) == ["playwright"]
    assert not mcp_path.exists()


def test_claude_review_uses_playwright_mcp_when_ui_review_enabled(tmp_path: Path) -> None:
    fake = _fake_claude_cli(tmp_path)
    package = "@playwright/mcp@0.0.84"
    config = _write_config(
        tmp_path,
        str(fake),
        ui_review={
            "enabled": True,
            "start_commands": ["npm start"],
            "url": "http://localhost:3000",
            "playwright_mcp_package": package,
        },
    )
    log_dir = tmp_path / "logs"
    runner = ClaudeAgentRunner(config, log_dir)
    workspace = tmp_path / "wt"
    workspace.mkdir()
    mcp_path = log_dir / "T1-review-claude-mcp.json"

    result = runner.review(
        _task(tmp_path), workspace, "HEAD", "verified", timeout_minutes=1
    )

    assert result.ok
    argv = json.loads((workspace / "fake-claude-argv.json").read_text(encoding="utf-8"))
    _assert_mcp_flags(argv, mcp_path)
    during = json.loads((workspace / "fake-claude-mcp.json").read_text(encoding="utf-8"))
    assert during["exists_during_call"] is True
    assert during["payload"] == {
        "mcpServers": {
            "playwright": {
                "command": "npx",
                "args": ["-y", package],
            }
        }
    }
    assert not mcp_path.exists()


def test_claude_implement_uses_empty_mcp_when_ui_review_disabled(tmp_path: Path) -> None:
    fake = _fake_claude_cli(tmp_path)
    config = _write_config(tmp_path, str(fake), ui_review={"enabled": False})
    log_dir = tmp_path / "logs"
    runner = ClaudeAgentRunner(config, log_dir)
    workspace = tmp_path / "wt"
    workspace.mkdir()
    mcp_path = log_dir / "T1-implement-claude-mcp.json"

    result = runner.implement(_task(tmp_path), workspace, timeout_minutes=1, attempt=1, previous_failure=None)

    assert result.ok
    argv = json.loads((workspace / "fake-claude-argv.json").read_text(encoding="utf-8"))
    _assert_mcp_flags(argv, mcp_path)
    during = json.loads((workspace / "fake-claude-mcp.json").read_text(encoding="utf-8"))
    assert during["exists_during_call"] is True
    assert during["payload"] == {"mcpServers": {}}
    assert not mcp_path.exists()


def test_claude_retry_uses_playwright_mcp_when_ui_review_enabled(tmp_path: Path) -> None:
    fake = _fake_claude_cli(tmp_path)
    package = "@playwright/mcp@0.0.99"
    config = _write_config(
        tmp_path,
        str(fake),
        ui_review={
            "enabled": True,
            "start_commands": ["npm start"],
            "url": "http://localhost:3000",
            "playwright_mcp_package": package,
        },
    )
    log_dir = tmp_path / "logs"
    runner = ClaudeAgentRunner(config, log_dir)
    workspace = tmp_path / "wt"
    workspace.mkdir()
    mcp_path = log_dir / "T1-implement-claude-mcp.json"

    result = runner.implement(
        _task(tmp_path),
        workspace,
        timeout_minutes=1,
        attempt=2,
        previous_failure="tests failed",
    )

    assert result.ok
    argv = json.loads((workspace / "fake-claude-argv.json").read_text(encoding="utf-8"))
    _assert_mcp_flags(argv, mcp_path)
    during = json.loads((workspace / "fake-claude-mcp.json").read_text(encoding="utf-8"))
    assert during["payload"]["mcpServers"]["playwright"]["args"] == ["-y", package]
    assert not mcp_path.exists()
