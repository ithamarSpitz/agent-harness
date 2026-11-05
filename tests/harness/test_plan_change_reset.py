from __future__ import annotations

import subprocess
from pathlib import Path

from harness.config import HarnessConfig
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.models import TaskStatus
from harness.plan_change import PlanChangeManager
from harness.roadmap import Roadmap
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier
from tests.portable import file_exists_command, yaml_quote


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class PlanRunner:
    def plan_change(self, *args, **kwargs):
        return AgentResult(True, "CHANGE_KIND: TASK_ADDITION\n")


class BudgetCheckingRunner:
    manages_model_routing = True

    def __init__(self, state: StateStore):
        self.state = state
        self.implement_calls = 0

    def max_implementation_attempts(self, task):
        return 1

    def has_implementation_budget(self, task, *, model_class_override=None):
        return self.state.get(task.id).provider_attempts.get("cursor", 0) < 1

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.implement_calls += 1
        (workspace / "out.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "fake success", provider="cursor")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


def test_plan_change_apply_resets_full_budget_and_scheduler_runs_task(tmp_path: Path):
    run(["git", "init", "-b", "main"], tmp_path)
    run(["git", "config", "user.name", "Test"], tmp_path)
    run(["git", "config", "user.email", "test@example.com"], tmp_path)
    write(tmp_path / ".gitignore", ".harness/\n")
    write(tmp_path / "AGENTS.md", "# test\n")
    write(tmp_path / "ARCHITECTURE.md", "# test\n")
    write(
        tmp_path / "harness.yaml",
        """
execution: {max_parallel_agents: 1, default_max_attempts: 3, default_timeout_minutes: 1}
cursor: {command: agent, output_format: text, models: {worker: null}}
verification: {global_task_commands: [git diff --check], stage_commands: []}
paths: {protected: [], review_required: []}
""",
    )
    verify_out = file_exists_command("out.txt")
    write(
        tmp_path / "tasks/A.md",
        f"""---
allowed_paths: [out.txt]
verification: [{yaml_quote(verify_out)}]
review: false
---
# A
""",
    )
    write(
        tmp_path / "tasks/roadmap.yaml",
        """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
""",
    )
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    config = HarnessConfig.load(tmp_path / "harness.yaml")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    failures = FailureStore(tmp_path / ".harness/failures")
    manager = PlanChangeManager(tmp_path, tmp_path / ".harness", state, failures)
    manager.ensure_baseline(roadmap)

    runtime = state.get("A")
    runtime.status = TaskStatus.BLOCKED
    runtime.attempt = 3
    runtime.provider_attempts = {"cursor": 3}
    runtime.stall_counts = {"cursor:implement": 2}
    runtime.started_at = "2026-01-01T00:00:00+00:00"
    runtime.finished_at = "2026-01-01T00:01:00+00:00"
    runtime.resource_launch_failures = 4
    state.set_meta("s1.A.provider.cursor.disabled", "quota:weekly")
    state.save()

    request = manager.propose(
        roadmap=roadmap,
        runner=PlanRunner(),
        sprint_id="s1",
        task_id="A",
        workspace=tmp_path,
        failure_summary="missing prerequisite",
        timeout_minutes=1,
        model_class="escalation",
    )
    manager.apply(request.id, roadmap, affected_task_ids=["A"], regenerate_contexts=False)

    reset = state.get("A")
    assert reset.status == TaskStatus.PENDING
    assert reset.attempt == 0
    assert reset.provider_attempts == {}
    assert reset.stall_counts == {}
    assert reset.started_at is None
    assert reset.finished_at is None
    assert reset.resource_launch_failures == 0
    assert state.get_meta("s1.A.provider.cursor.disabled") is None

    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    verifier = Verifier(config, tmp_path / ".harness/logs", worktrees)
    runner = BudgetCheckingRunner(state)
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert runner.implement_calls == 1
    assert state.get("A").status == TaskStatus.DONE
