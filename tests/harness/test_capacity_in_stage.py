from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from harness.config import HarnessConfig
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.models import TaskStatus
from harness.roadmap import Roadmap
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier
from tests.portable import file_exists_command, yaml_quote


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class AdvancingClock:
    def __init__(self, step: float):
        self.value = 0.0
        self.step = step
        self.lock = threading.Lock()

    def __call__(self) -> float:
        with self.lock:
            value = self.value
            self.value += self.step
            return value


class CapacityThenSuccessRunner:
    def __init__(self, clock: AdvancingClock):
        self.clock = clock
        self.calls = {"A": 0, "B": 0}
        self.a_finished = threading.Event()
        self.finished_at: dict[str, float] = {}
        self.lock = threading.Lock()

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        with self.lock:
            self.calls[task.id] += 1
            call = self.calls[task.id]
        if task.id == "A" and call == 1:
            return AgentResult(False, "temporary network outage", "fake", waiting_cause="network")
        if task.id == "B":
            assert self.a_finished.wait(timeout=30)
        (workspace / f"out-{task.id.lower()}.txt").write_text("ok\n", encoding="utf-8")
        self.finished_at[task.id] = self.clock()
        if task.id == "A":
            self.a_finished.set()
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


class CapacityWithCompletedSiblingRunner:
    def __init__(self):
        self.calls = {"A": 0, "B": 0}
        self.lock = threading.Lock()

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        with self.lock:
            self.calls[task.id] += 1
            call = self.calls[task.id]
        if task.id == "A":
            assert call == 1, "capacity-paused task restarted without an active sibling"
            return AgentResult(False, "temporary network outage", "fake", waiting_cause="network")
        (workspace / "out-b.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


def init_repo(
    root: Path, task_ids: tuple[str, ...] = ("A", "B"),
) -> tuple[HarnessConfig, Roadmap, StateStore, WorktreeManager, Verifier, FailureStore]:
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    write(root / ".gitignore", ".harness/\n")
    write(root / "AGENTS.md", "# test\n")
    write(root / "ARCHITECTURE.md", "# architecture\n")
    write(
        root / "harness.yaml",
        """
execution:
  max_parallel_agents: 2
  default_max_attempts: 1
  default_timeout_minutes: 1
  watchdog_poll_seconds: 1
cursor:
  command: agent
  output_format: text
  models: {worker: null, reviewer: null, escalation: null}
capacity:
  retry_interval_minutes: 5
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths: {protected: [], review_required: []}
""",
    )
    checks: list[str] = []
    for task_id in task_ids:
        output = f"out-{task_id.lower()}.txt"
        check = file_exists_command(output)
        checks.append(check)
        write(
            root / f"tasks/{task_id}.md",
            f"""---
allowed_paths: [{output}]
verification: [{yaml_quote(check)}]
review: false
---
# {task_id}
""",
        )
    task_rows = "\n".join(
        f"      {task_id}: {{file: tasks/{task_id}.md, stage: 1, depends_on: []}}"
        for task_id in task_ids
    )
    write(
        root / "tasks/roadmap.yaml",
        f"""
project: test
sprints:
  s1:
    tasks:
{task_rows}
    stage_verification:
      "1": [{', '.join(yaml_quote(check) for check in checks)}]
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
    return config, roadmap, state, worktrees, verifier, failures


def test_capacity_pause_retries_while_stage_sibling_is_running(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path)
    clock = AdvancingClock(300.0)
    runner = CapacityThenSuccessRunner(clock)
    scheduler = Scheduler(
        tmp_path,
        config,
        state,
        worktrees,
        runner,
        verifier,
        failures=failures,
        clock=clock,
    )

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert runner.calls == {"A": 2, "B": 1}
    assert runner.finished_at["A"] < runner.finished_at["B"]
    assert state.get("A").attempt == 1
    assert state.get("A").status == TaskStatus.DONE


def test_due_capacity_retry_returns_wait_when_all_unfinished_tasks_are_paused(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path)
    state.get("B").status = TaskStatus.DONE
    state.save()
    clock = AdvancingClock(300.0)
    runner = CapacityWithCompletedSiblingRunner()
    scheduler = Scheduler(
        tmp_path,
        config,
        state,
        worktrees,
        runner,
        verifier,
        failures=failures,
        clock=clock,
    )

    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert runner.calls == {"A": 1, "B": 0}
    assert state.get("A").attempt == 0
    assert state.get("A").status == TaskStatus.WAITING_NETWORK
    assert state.get("B").status == TaskStatus.DONE
    assert failures.get("s1")["kind"] == "CAPACITY_WAIT"


def test_due_capacity_retry_takes_first_free_slot_ahead_of_backlog(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path, ("A", "B", "C"))
    config.execution.max_parallel_agents = 1
    clock = AdvancingClock(0.0)
    calls: list[str] = []
    attempts: list[int] = []

    class BacklogRunner:
        def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
            calls.append(task.id)
            if task.id == "A":
                attempts.append(attempt)
                if len(attempts) == 1:
                    return AgentResult(False, "network outage", "fake", waiting_cause="network")
            if task.id == "B":
                assert state.get("A").status == TaskStatus.WAITING_NETWORK
                assert state.get("A").attempt == 0
                # B occupies the only worker until after A's retry deadline.
                with clock.lock:
                    clock.value += 600.0
            (workspace / f"out-{task.id.lower()}.txt").write_text("ok\n", encoding="utf-8")
            return AgentResult(True, "ok")

    scheduler = Scheduler(
        tmp_path, config, state, worktrees, BacklogRunner(), verifier,
        failures=failures, clock=clock,
    )

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert calls == ["A", "B", "A", "C"]
    assert attempts == [1, 1]
    assert all(state.get(task_id).status == TaskStatus.DONE for task_id in ("A", "B", "C"))
