from __future__ import annotations

import json
import subprocess
from pathlib import Path

import harness.outcomes as outcomes
from harness.config import HarnessConfig
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.models import TaskStatus
from harness.roadmap import Roadmap
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.usage import UsageRecorder
from harness.verifier import Verifier
from tests.portable import file_exists_command, yaml_quote


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _enable_shared(tmp_home: Path, monkeypatch) -> Path:
    folder = tmp_home / ".harness"
    monkeypatch.setattr(outcomes, "_folder_configured", True)
    monkeypatch.setattr(outcomes, "_folder_override", folder)
    return folder


def setup_repo(root: Path, *, max_attempts: int = 2):
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    write(root / ".gitignore", ".harness/\n")
    write(root / "AGENTS.md", "# test\n")
    write(
        root / "harness.yaml",
        f"""
execution:
  max_parallel_agents: 1
  default_max_attempts: {max_attempts}
  default_timeout_minutes: 1
cursor:
  command: agent
  output_format: text
  models: {{worker: cheap-model, reviewer: review-model}}
retry:
  sequence: []
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths: {{protected: [], review_required: []}}
""",
    )
    verify_out = file_exists_command("out.txt")
    write(
        root / "tasks/A.md",
        f"""---
id: A
stage: 1
model_class: worker
review: false
allowed_paths: [out.txt]
verification: [{yaml_quote(verify_out)}]
---
# A
""",
    )
    write(
        root / "tasks/roadmap.yaml",
        f"""
project: test
sprints:
  s1:
    tasks:
      A: {{file: tasks/A.md, stage: 1, depends_on: []}}
    stage_verification:
      "1": [{yaml_quote(verify_out)}]
""",
    )
    run(["git", "add", "."], root)
    run(["git", "commit", "-m", "initial"], root)
    config = HarnessConfig.load(root / "harness.yaml")
    roadmap = Roadmap(root, root / "tasks/roadmap.yaml")
    state = StateStore(root / ".harness/state.json")
    worktrees = WorktreeManager(root, root / ".harness")
    verifier = Verifier(config, root / ".harness/logs", worktrees)
    failures = FailureStore(root / ".harness/failures")
    usage = UsageRecorder(root / ".harness/usage.jsonl")
    return config, roadmap, state, worktrees, verifier, failures, usage


def _task_final_records(shared: Path) -> list[dict]:
    path = shared / "outcomes.jsonl"
    if not path.exists():
        return []
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [record for record in records if record.get("event_type") == "task_final"]


class _FailSonnetThenPass:
    """First implement fails as sonnet/medium; second succeeds as sonnet/high."""

    def __init__(self, usage: UsageRecorder) -> None:
        self.usage = usage
        self.calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.usage.record(
                {
                    "task_id": task.id,
                    "phase": "implement",
                    "attempt": attempt,
                    "model_class": task.model_class,
                    "provider": "claude",
                    "model": "sonnet",
                    "effort": "medium",
                    "ok": False,
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "duration_seconds": 1.5,
                }
            )
            return AgentResult(False, "", "sonnet medium failed", provider="claude")
        (workspace / "out.txt").write_text("ok\n", encoding="utf-8")
        self.usage.record(
            {
                "task_id": task.id,
                "phase": "implement",
                "attempt": attempt,
                "model_class": task.model_class,
                "provider": "claude",
                "model": "sonnet",
                "effort": "high",
                "ok": True,
                "input_tokens": 200,
                "output_tokens": 40,
                "duration_seconds": 2.5,
            }
        )
        return AgentResult(True, "ok", provider="claude")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n", provider="claude")


class _AlwaysFailRecording:
    def __init__(self, usage: UsageRecorder) -> None:
        self.usage = usage
        self.calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.calls += 1
        efforts = ("medium", "high")
        effort = efforts[min(self.calls - 1, len(efforts) - 1)]
        self.usage.record(
            {
                "task_id": task.id,
                "phase": "implement",
                "attempt": attempt,
                "model_class": task.model_class,
                "provider": "claude",
                "model": "sonnet",
                "effort": effort,
                "ok": False,
                "input_tokens": 50 * self.calls,
                "output_tokens": 10 * self.calls,
                "duration_seconds": float(self.calls),
            }
        )
        return AgentResult(False, "", f"failed attempt {attempt}", provider="claude")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n", provider="claude")


def test_done_after_sonnet_medium_failure_records_both_attempts(
    tmp_path: Path, monkeypatch
) -> None:
    shared = _enable_shared(tmp_path / "home", monkeypatch)
    config, roadmap, state, worktrees, verifier, failures, usage = setup_repo(tmp_path)
    runner = _FailSonnetThenPass(usage)
    scheduler = Scheduler(
        tmp_path, config, state, worktrees, runner, verifier, failures=failures
    )

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.DONE
    assert runner.calls == 2

    finals = _task_final_records(shared)
    assert len(finals) == 1
    record = finals[0]
    assert record["event_type"] == "task_final"
    assert record["project"] == str(tmp_path.resolve())
    assert record["sprint"] == "s1"
    assert record["task_id"] == "A"
    assert record["model_class"] == "worker"
    assert record["final_status"] == "DONE"
    assert record["implementation_attempts"] == 2
    assert record["attempts"] == [
        {"provider": "claude", "model": "sonnet", "effort": "medium"},
        {"provider": "claude", "model": "sonnet", "effort": "high"},
    ]
    assert record["passing_attempt"] == {
        "provider": "claude",
        "model": "sonnet",
        "effort": "high",
    }
    assert record["total_tokens"] == 360
    assert record["total_duration_seconds"] == 4.0


def test_blocked_task_records_all_attempts(tmp_path: Path, monkeypatch) -> None:
    shared = _enable_shared(tmp_path / "home", monkeypatch)
    config, roadmap, state, worktrees, verifier, failures, usage = setup_repo(
        tmp_path, max_attempts=2
    )
    runner = _AlwaysFailRecording(usage)
    scheduler = Scheduler(
        tmp_path, config, state, worktrees, runner, verifier, failures=failures
    )

    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.BLOCKED
    assert runner.calls == 2

    finals = _task_final_records(shared)
    assert len(finals) == 1
    record = finals[0]
    assert record["final_status"] == "BLOCKED"
    assert record["implementation_attempts"] == 2
    assert record["attempts"] == [
        {"provider": "claude", "model": "sonnet", "effort": "medium"},
        {"provider": "claude", "model": "sonnet", "effort": "high"},
    ]
    assert record["passing_attempt"] is None
    assert record["total_tokens"] == 180
    assert record["total_duration_seconds"] == 3.0
