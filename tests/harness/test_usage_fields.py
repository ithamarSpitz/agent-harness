from __future__ import annotations

import json
from pathlib import Path

import yaml

from harness.codex_runner import CodexAgentRunner
from harness.config import HarnessConfig
from harness.models import TaskSpec
from harness.runner import CursorAgentRunner
from harness.scheduler import Scheduler
from harness.usage import UsageRecorder, parse_review_outcome
from tests.portable import make_python_script, yaml_quote


def _task(tmp_path: Path, *, task_id: str = "S9-T18", sprint: str = "S9") -> TaskSpec:
    task_file = tmp_path / "task.md"
    task_file.write_text("# task\n", encoding="utf-8")
    return TaskSpec(
        id=task_id,
        file=task_file,
        sprint=sprint,
        stage=3,
        model_class="worker",
        review=True,
        allowed_paths=["**"],
    )


def _codex_config(tmp_path: Path, command: str) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "execution": {
                    "stall_timeout_minutes": 0,
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
                    "command": command,
                    "reasoning_effort": "high",
                    "implementation_sequence": [
                        {"model": "gpt-test", "reasoning_effort": "medium"},
                    ],
                    "review_model": "gpt-review",
                },
                "verification": {"global_task_commands": []},
                "paths": {"protected": [], "review_required": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return HarnessConfig.load(path)


def _cursor_config(tmp_path: Path, command: str) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(
        f"""
execution:
  stall_timeout_minutes: 0
  watchdog_poll_seconds: 0.01
cursor:
  command: {yaml_quote(command)}
  output_format: text
  models: {{worker: cursor-test, reviewer: cursor-review}}
verification: {{}}
paths: {{}}
""",
        encoding="utf-8",
    )
    return HarnessConfig.load(path)


def _fake_codex_cli(tmp_path: Path) -> Path:
    """Fake Codex: implement succeeds; review returns prompt-compliant FAIL + IMPACT."""
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
(workspace / "fake-codex-argv.json").write_text(json.dumps(args), encoding="utf-8")

review_text = (
    "VERDICT: FAIL\n"
    "harness/usage.py: missing sprint on implement records\n"
    "harness/runner.py: review lacks finding_count\n"
    "IMPACT:\n"
    "- checked UsageRecorder callers\n"
    "- none outside the diff\n"
    "UI_CHECK: skipped - no UI impact\n"
)
is_review = "VERDICT: PASS" in prompt or "VERDICT: FAIL" in prompt
text = review_text if is_review else "implemented"
print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
print(json.dumps({
    "type": "turn.completed",
    "usage": {
        "input_tokens": 11,
        "cached_input_tokens": 3,
        "output_tokens": 7,
        "reasoning_output_tokens": 2,
    },
}))
''',
    )


def _fake_cursor_agent(tmp_path: Path) -> Path:
    return make_python_script(
        tmp_path / "fake-agent.py",
        """import sys
args = sys.argv[1:]
if "--version" in args or "--help" in args:
    print("fake-agent 1.0")
    raise SystemExit(0)
if "--mode=ask" in args:
    print("VERDICT: PASS")
    print("IMPACT:")
    print("- checked callers")
    print("UI_CHECK: skipped - no UI")
else:
    print("implementation complete")
""",
    )


def test_parse_review_outcome_excludes_impact_and_ui_check_sections() -> None:
    body = (
        "VERDICT: FAIL\n"
        "first finding\n"
        "second finding\n"
        "IMPACT:\n"
        "- checked callers of UsageRecorder\n"
        "- none outside the diff\n"
        "UI_CHECK: skipped - no UI impact\n"
    )
    assert parse_review_outcome(True, body) == ("FAIL", 2)
    assert parse_review_outcome(True, "VERDICT: PASS\nIMPACT:\n- ok\n") == ("PASS", 0)


def test_codex_implement_and_review_record_sprint_effort_and_findings(tmp_path: Path) -> None:
    cli = _fake_codex_cli(tmp_path)
    usage = UsageRecorder(tmp_path / ".harness" / "usage.jsonl")
    runner = CodexAgentRunner(_codex_config(tmp_path, str(cli)), tmp_path / "logs", usage=usage)
    task = _task(tmp_path)
    workspace = tmp_path / "wt"
    workspace.mkdir()

    implement = runner.implement(
        task, workspace, timeout_minutes=1, attempt=2, previous_failure=None,
        model="gpt-test", reasoning_effort="high",
    )
    assert implement.ok

    review = runner.review(
        task, workspace, base_ref="HEAD", verification_summary="ok",
        timeout_minutes=1, model="gpt-review",
    )
    assert review.ok

    events = usage.events()
    assert len(events) == 2
    implement_event, review_event = events

    assert implement_event["phase"] == "implement"
    assert implement_event["sprint"] == "S9"
    assert implement_event["effort"] == "high"

    assert review_event["phase"] == "review"
    assert review_event["sprint"] == "S9"
    assert review_event["effort"] == "high"  # review uses config.codex.reasoning_effort
    assert review_event["review_verdict"] == "FAIL"
    assert review_event["finding_count"] == 2

    argv = json.loads((workspace / "fake-codex-argv.json").read_text(encoding="utf-8"))
    assert 'model_reasoning_effort="high"' in argv


def test_cursor_implement_and_review_record_sprint_null_effort_and_pass(
    tmp_path: Path,
) -> None:
    fake = _fake_cursor_agent(tmp_path)
    usage = UsageRecorder(tmp_path / ".harness" / "usage.jsonl")
    runner = CursorAgentRunner(_cursor_config(tmp_path, str(fake)), tmp_path / "logs", usage=usage)
    task = _task(tmp_path)
    workspace = tmp_path / "wt"
    workspace.mkdir()

    implement = runner.implement(
        task, workspace, timeout_minutes=1, attempt=1, previous_failure=None,
    )
    assert implement.ok

    review = runner.review(
        task, workspace, base_ref="HEAD", verification_summary="ok", timeout_minutes=1,
    )
    assert review.ok

    events = usage.events()
    assert len(events) == 2
    implement_event, review_event = events

    assert implement_event["phase"] == "implement"
    assert implement_event["sprint"] == "S9"
    assert implement_event["effort"] is None

    assert review_event["phase"] == "review"
    assert review_event["sprint"] == "S9"
    assert review_event["effort"] is None
    assert review_event["review_verdict"] == "PASS"
    assert review_event["finding_count"] == 0


def test_scheduler_records_verification_result(tmp_path: Path) -> None:
    usage = UsageRecorder(tmp_path / ".harness" / "usage.jsonl")
    scheduler = object.__new__(Scheduler)
    scheduler.usage = usage

    scheduler._record_verification_usage(_task(tmp_path), 2, False, 1.23456)

    event = usage.events()[0]
    assert event == {
        "attempt": 2,
        "duration_seconds": 1.235,
        "model": "verification",
        "model_class": "worker",
        "ok": False,
        "phase": "verification",
        "provider": "harness",
        "recorded_at": event["recorded_at"],
        "sprint": "S9",
        "task_id": "S9-T18",
        "verification_ok": False,
    }
