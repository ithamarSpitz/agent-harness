from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from harness.config import HarnessConfig
from harness.models import TaskSpec
from harness.runner import CursorAgentRunner
from tests.portable import make_python_script, yaml_quote


def _task(tmp_path: Path) -> TaskSpec:
    task_file = tmp_path / "task.md"
    task_file.write_text("# task\n", encoding="utf-8")
    return TaskSpec(id="T1", file=task_file, sprint="S1", stage=1, review=True, allowed_paths=["**"])


def _fake_cursor_agent(tmp_path: Path, *, help_text: str) -> Path:
    return make_python_script(
        tmp_path / "fake-agent.py",
        f"""import json, os, sys
if "--help" in sys.argv:
    print({help_text!r})
    raise SystemExit(0)
log = os.environ.get("ARG_LOG")
if log:
    with open(log, "w", encoding="utf-8") as fh:
        json.dump(sys.argv[1:], fh)
print("VERDICT: PASS")
""",
    )


def _config_yaml(tmp_path: Path, fake: Path, *, ui_review_enabled: bool) -> Path:
    ui_block = ""
    if ui_review_enabled:
        ui_block = """
ui_review:
  enabled: true
  start_commands: [npm start]
  url: http://localhost:1
"""
    path = tmp_path / "harness.yaml"
    path.write_text(
        f"""
execution:
  stall_timeout_minutes: 0
  watchdog_poll_seconds: 0.01
worktree: {{}}
cursor:
  command: {yaml_quote(fake)}
  output_format: text
  models: {{worker: cheap, reviewer: cheap, escalation: cheap}}
verification: {{}}
paths: {{}}
context: {{}}
doctor: {{}}
{ui_block}""",
        encoding="utf-8",
    )
    return path


def _invoke_args(
    tmp_path: Path,
    fake: Path,
    *,
    phase: str,
    ui_review_enabled: bool,
) -> list[str]:
    config = HarnessConfig.load(_config_yaml(tmp_path, fake, ui_review_enabled=ui_review_enabled))
    runner = CursorAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "wt"
    workspace.mkdir()
    env = os.environ.copy()
    arg_log = tmp_path / f"{phase}-args.json"
    env["ARG_LOG"] = str(arg_log)
    result = runner._invoke(
        task_id="T1",
        phase=phase,
        prompt="x",
        workspace=workspace,
        model_class="worker" if phase == "implement" else "reviewer",
        timeout_minutes=1,
        log_name=f"{phase}.log",
        mode="ask" if phase != "implement" else None,
        env=env,
    )
    assert result.ok
    return json.loads(arg_log.read_text(encoding="utf-8"))


@pytest.mark.parametrize("phase", ["implement", "review"])
def test_cursor_adds_approve_mcps_for_implement_and_review_when_ui_review_enabled(
    tmp_path: Path, phase: str,
):
    fake = _fake_cursor_agent(
        tmp_path,
        help_text="Usage: agent [--approve-mcps] [--trust]\\n",
    )
    args = _invoke_args(tmp_path, fake, phase=phase, ui_review_enabled=True)
    assert "--approve-mcps" in args


@pytest.mark.parametrize("phase", ["implement", "review"])
def test_cursor_omits_approve_mcps_when_ui_review_disabled(tmp_path: Path, phase: str):
    fake = _fake_cursor_agent(
        tmp_path,
        help_text="Usage: agent [--approve-mcps] [--trust]\\n",
    )
    args = _invoke_args(tmp_path, fake, phase=phase, ui_review_enabled=False)
    assert "--approve-mcps" not in args


def test_cursor_plan_repair_omits_approve_mcps_even_when_ui_review_enabled(tmp_path: Path):
    fake = _fake_cursor_agent(
        tmp_path,
        help_text="Usage: agent [--approve-mcps] [--trust]\\n",
    )
    args = _invoke_args(tmp_path, fake, phase="plan_repair", ui_review_enabled=True)
    assert "--approve-mcps" not in args


def test_missing_playwright_mcp_logs_warning_once_per_runner(tmp_path: Path, monkeypatch, capsys):
    mcp_path = tmp_path / "mcp.json"
    mcp_path.write_text('{"mcpServers": {}}', encoding="utf-8")
    monkeypatch.setattr("harness.runner._cursor_mcp_config_path", lambda: mcp_path)

    fake = _fake_cursor_agent(
        tmp_path,
        help_text="Usage: agent [--approve-mcps]\\n",
    )
    config = HarnessConfig.load(_config_yaml(tmp_path, fake, ui_review_enabled=True))
    runner = CursorAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "wt"
    workspace.mkdir()
    env = os.environ.copy()

    for attempt in (1, 2):
        arg_log = tmp_path / f"attempt-{attempt}.json"
        env["ARG_LOG"] = str(arg_log)
        result = runner.implement(
            _task(tmp_path), workspace, timeout_minutes=1, attempt=attempt, previous_failure=None, env=env,
        )
        assert result.ok

    captured = capsys.readouterr()
    assert captured.err.count("WARNING:") == 1
    assert str(mcp_path) in captured.err
