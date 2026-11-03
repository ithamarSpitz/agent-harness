from __future__ import annotations

import os
from pathlib import Path

import yaml

from harness.codex_runner import CodexAgentRunner
from harness.config import HarnessConfig
from harness.models import TaskSpec
from harness.runner import CursorAgentRunner
from tests.portable import make_python_script, yaml_quote

UI_COMMAND = "python -m fake_app --port 4321"
UI_URL = "http://127.0.0.1:4321/example"


def _task(tmp_path: Path) -> TaskSpec:
    task_file = tmp_path / "task.md"
    task_file.write_text("# task\n", encoding="utf-8")
    return TaskSpec(id="T1", file=task_file, sprint="S1", stage=1, review=True, allowed_paths=["**"])


def _cursor_config_yaml(tmp_path: Path, fake: Path, *, ui_review_enabled: bool) -> Path:
    ui_block = ""
    if ui_review_enabled:
        ui_block = f"""
ui_review:
  enabled: true
  start_commands: [{yaml_quote(UI_COMMAND)}]
  url: {yaml_quote(UI_URL)}
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


def _codex_config_yaml(tmp_path: Path, fake: Path, *, ui_review_enabled: bool) -> Path:
    payload: dict = {
        "execution": {
            "stall_timeout_minutes": 0.1,
            "watchdog_poll_seconds": 0.01,
        },
        "cursor": {
            "enabled": True,
            "command": "agent",
            "models": {"worker": "composer-2.5", "reviewer": "composer-2.5"},
        },
        "providers": {"priority": ["codex", "cursor"]},
        "codex": {
            "enabled": True,
            "command": str(fake),
            "implementation_sequence": ["gpt-5.6-sol"],
            "review_model": "gpt-5.6-sol",
        },
        "retry": {"sequence": ["worker"]},
        "verification": {"global_task_commands": []},
        "paths": {"protected": [], "review_required": []},
    }
    if ui_review_enabled:
        payload["ui_review"] = {
            "enabled": True,
            "start_commands": [UI_COMMAND],
            "url": UI_URL,
        }
    path = tmp_path / "harness.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _fake_cursor_agent(tmp_path: Path) -> Path:
    return make_python_script(
        tmp_path / "fake-agent.py",
        """import json, os, sys
if "--help" in sys.argv:
    print("Usage: agent [--approve-mcps] [--trust]")
    raise SystemExit(0)
prompt_log = os.environ.get("PROMPT_LOG")
if prompt_log:
    prompt = ""
    if "-p" in sys.argv:
        idx = sys.argv.index("-p")
        # Prompt may be the next argv entry, or streamed on stdin (Windows .cmd).
        if idx + 1 < len(sys.argv) and not sys.argv[idx + 1].startswith("-"):
            prompt = sys.argv[idx + 1]
        else:
            prompt = sys.stdin.read()
    else:
        prompt = sys.stdin.read()
    with open(prompt_log, "w", encoding="utf-8") as fh:
        fh.write(prompt)
print("STATUS\\nCHANGED_FILES\\nTEST_RESULTS\\nOPEN_ISSUES")
""",
    )


def _fake_codex_cli(tmp_path: Path) -> Path:
    return make_python_script(
        tmp_path / "fake-codex.py",
        r'''from __future__ import annotations
import json, sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.154.0")
    raise SystemExit(0)
if args == ["login", "status"]:
    print("Logged in using ChatGPT")
    raise SystemExit(0)
if not args or args[0] != "exec":
    raise SystemExit(2)

prompt = sys.stdin.read()
workspace = Path(args[args.index("--cd") + 1])
(workspace / "fake-codex-stdin.txt").write_text(prompt, encoding="utf-8")
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}))
print(json.dumps({"type":"turn.completed","usage":{}}))
''',
    )


def _assert_ui_instruction_present(prompt: str) -> None:
    assert "use the Playwright tools" in prompt
    assert UI_COMMAND in prompt
    assert UI_URL in prompt


def _assert_no_browser_instruction(prompt: str) -> None:
    assert "Playwright" not in prompt
    assert "browser" not in prompt
    assert "computer-use" not in prompt
    assert UI_COMMAND not in prompt
    assert UI_URL not in prompt


def test_cursor_implement_prompt_includes_ui_review_when_enabled(tmp_path: Path) -> None:
    fake = _fake_cursor_agent(tmp_path)
    config = HarnessConfig.load(_cursor_config_yaml(tmp_path, fake, ui_review_enabled=True))
    runner = CursorAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "wt"
    workspace.mkdir()
    env = os.environ.copy()
    prompt_log = tmp_path / "implement-prompt.txt"
    env["PROMPT_LOG"] = str(prompt_log)

    result = runner.implement(
        _task(tmp_path), workspace, timeout_minutes=1, attempt=1, previous_failure=None, env=env,
    )
    assert result.ok
    _assert_ui_instruction_present(prompt_log.read_text(encoding="utf-8"))


def test_cursor_retry_prompt_includes_ui_review_when_enabled(tmp_path: Path) -> None:
    fake = _fake_cursor_agent(tmp_path)
    config = HarnessConfig.load(_cursor_config_yaml(tmp_path, fake, ui_review_enabled=True))
    runner = CursorAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "wt"
    workspace.mkdir()
    env = os.environ.copy()
    prompt_log = tmp_path / "retry-prompt.txt"
    env["PROMPT_LOG"] = str(prompt_log)

    result = runner.implement(
        _task(tmp_path),
        workspace,
        timeout_minutes=1,
        attempt=2,
        previous_failure="verification failed",
        env=env,
    )
    assert result.ok
    prompt = prompt_log.read_text(encoding="utf-8")
    _assert_ui_instruction_present(prompt)
    assert "verification failed" in prompt


def test_cursor_implement_prompt_omits_ui_when_disabled(tmp_path: Path) -> None:
    fake = _fake_cursor_agent(tmp_path)
    config = HarnessConfig.load(_cursor_config_yaml(tmp_path, fake, ui_review_enabled=False))
    runner = CursorAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "wt"
    workspace.mkdir()
    env = os.environ.copy()
    prompt_log = tmp_path / "implement-prompt.txt"
    env["PROMPT_LOG"] = str(prompt_log)

    result = runner.implement(
        _task(tmp_path), workspace, timeout_minutes=1, attempt=1, previous_failure=None, env=env,
    )
    assert result.ok
    _assert_no_browser_instruction(prompt_log.read_text(encoding="utf-8"))


def test_codex_implement_prompt_includes_ui_review_when_enabled(tmp_path: Path) -> None:
    fake = _fake_codex_cli(tmp_path)
    config = HarnessConfig.load(_codex_config_yaml(tmp_path, fake, ui_review_enabled=True))
    runner = CodexAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "wt"
    workspace.mkdir()

    result = runner.implement(
        _task(tmp_path), workspace, 1, 1, None, model="gpt-5.6-sol",
    )
    assert result.ok
    prompt = (workspace / "fake-codex-stdin.txt").read_text(encoding="utf-8")
    _assert_ui_instruction_present(prompt)


def test_codex_retry_prompt_includes_ui_review_when_enabled(tmp_path: Path) -> None:
    fake = _fake_codex_cli(tmp_path)
    config = HarnessConfig.load(_codex_config_yaml(tmp_path, fake, ui_review_enabled=True))
    runner = CodexAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "wt"
    workspace.mkdir()

    result = runner.implement(
        _task(tmp_path), workspace, 1, 2, "verification failed", model="gpt-5.6-sol",
    )
    assert result.ok
    prompt = (workspace / "fake-codex-stdin.txt").read_text(encoding="utf-8")
    _assert_ui_instruction_present(prompt)
    assert "verification failed" in prompt


def test_codex_implement_prompt_omits_ui_when_disabled(tmp_path: Path) -> None:
    fake = _fake_codex_cli(tmp_path)
    config = HarnessConfig.load(_codex_config_yaml(tmp_path, fake, ui_review_enabled=False))
    runner = CodexAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "wt"
    workspace.mkdir()

    result = runner.implement(
        _task(tmp_path), workspace, 1, 1, None, model="gpt-5.6-sol",
    )
    assert result.ok
    prompt = (workspace / "fake-codex-stdin.txt").read_text(encoding="utf-8")
    _assert_no_browser_instruction(prompt)
