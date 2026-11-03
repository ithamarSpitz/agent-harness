from __future__ import annotations

import logging
import subprocess
import threading
import time
from pathlib import Path

from harness.agent_ledger import LedgerStore
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


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def init_repo(
    root: Path,
    task_ids: tuple[str, ...] = ("A", "B", "C"),
    *,
    max_parallel: int = 3,
    memory_per_agent_gb: float = 1.5,
    min_free_memory_gb: float = 2.0,
) -> tuple[HarnessConfig, Roadmap, StateStore, WorktreeManager, Verifier, FailureStore]:
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    write(root / ".gitignore", ".harness/\n")
    write(root / "AGENTS.md", "# test\n")
    write(
        root / "harness.yaml",
        f"""
execution:
  max_parallel_agents: {max_parallel}
  default_max_attempts: 1
  default_timeout_minutes: 1
  watchdog_poll_seconds: 1
  min_free_memory_gb: {min_free_memory_gb}
  memory_per_agent_gb: {memory_per_agent_gb}
cursor:
  command: agent
  output_format: text
  models: {{worker: null, reviewer: null, escalation: null}}
capacity:
  retry_interval_minutes: 5
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths: {{protected: [], review_required: []}}
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


class TrackingRunner:
    """Fake agent that records concurrency and can hold tasks until released."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.started: list[str] = []
        self.finished: list[str] = []
        self._started_events: dict[str, threading.Event] = {}
        self._release_events: dict[str, threading.Event] = {}

    def gate(self, *task_ids: str) -> None:
        for task_id in task_ids:
            self._started_events[task_id] = threading.Event()
            self._release_events[task_id] = threading.Event()

    def wait_started(self, task_id: str, timeout: float = 5.0) -> None:
        assert self._started_events[task_id].wait(timeout=timeout)

    def release(self, *task_ids: str) -> None:
        targets = task_ids or tuple(self._release_events)
        for task_id in targets:
            event = self._release_events.get(task_id)
            if event is not None:
                event.set()

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.started.append(task.id)
        started = self._started_events.get(task.id)
        if started is not None:
            started.set()
        release = self._release_events.get(task.id)
        if release is not None:
            assert release.wait(timeout=10)
        time.sleep(0.05)
        (workspace / f"out-{task.id.lower()}.txt").write_text("ok\n", encoding="utf-8")
        with self.lock:
            self.active -= 1
            self.finished.append(task.id)
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


def _scheduler(
    root: Path,
    config: HarnessConfig,
    state: StateStore,
    worktrees: WorktreeManager,
    runner: TrackingRunner,
    verifier: Verifier,
    failures: FailureStore,
    *,
    free_memory_gb,
) -> Scheduler:
    # Keep the ledger under gitignored .harness/ so stage merges stay clean.
    ledger_folder = root / ".harness" / "test-ledger"
    return Scheduler(
        root,
        config,
        state,
        worktrees,
        runner,
        verifier,
        failures=failures,
        free_memory_gb=free_memory_gb,
        ledger=LedgerStore(ledger_folder, process_alive=lambda _pid: True),
    )


def test_plenty_of_memory_reaches_max_parallel(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(
        tmp_path, max_parallel=3, memory_per_agent_gb=1.0, min_free_memory_gb=1.0
    )
    runner = TrackingRunner()
    runner.gate("A", "B", "C")
    free = {"gb": 100.0}
    scheduler = _scheduler(
        tmp_path, config, state, worktrees, runner, verifier, failures,
        free_memory_gb=lambda: free["gb"],
    )

    thread = threading.Thread(target=lambda: scheduler.run_sprint(roadmap.sprint("s1")))
    thread.start()
    for task_id in ("A", "B", "C"):
        runner.wait_started(task_id)
    assert runner.peak == 3
    runner.release()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert all(state.get(task_id).status == TaskStatus.DONE for task_id in ("A", "B", "C"))


def test_memory_for_one_runs_serially(tmp_path: Path):
    # free - min_free = 2.5; estimate 2.0 → second agent blocked while first is warm.
    config, roadmap, state, worktrees, verifier, failures = init_repo(
        tmp_path, ("A", "B"), max_parallel=3, memory_per_agent_gb=2.0, min_free_memory_gb=2.0
    )
    runner = TrackingRunner()
    runner.gate("A")
    scheduler = _scheduler(
        tmp_path, config, state, worktrees, runner, verifier, failures,
        free_memory_gb=lambda: 4.5,
    )

    thread = threading.Thread(target=lambda: scheduler.run_sprint(roadmap.sprint("s1")))
    thread.start()
    runner.wait_started("A")
    time.sleep(0.3)
    assert runner.peak == 1
    assert "B" not in runner.started
    runner.release()
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert runner.peak == 1
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("B").status == TaskStatus.DONE


def test_almost_no_memory_still_starts_one(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(
        tmp_path, ("A", "B"), max_parallel=3, memory_per_agent_gb=2.0, min_free_memory_gb=2.0
    )
    runner = TrackingRunner()
    scheduler = _scheduler(
        tmp_path, config, state, worktrees, runner, verifier, failures,
        free_memory_gb=lambda: 0.1,
    )

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert runner.peak == 1
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("B").status == TaskStatus.DONE


def test_finished_task_frees_slot_for_next_pending(tmp_path: Path):
    # Enough free memory for two warm agents (2*2 + 2 = 6), not three.
    config, roadmap, state, worktrees, verifier, failures = init_repo(
        tmp_path, ("A", "B", "C"), max_parallel=3, memory_per_agent_gb=2.0, min_free_memory_gb=2.0
    )
    runner = TrackingRunner()
    runner.gate("A", "B", "C")
    scheduler = _scheduler(
        tmp_path, config, state, worktrees, runner, verifier, failures,
        free_memory_gb=lambda: 6.0,
    )

    thread = threading.Thread(target=lambda: scheduler.run_sprint(roadmap.sprint("s1")))
    thread.start()
    runner.wait_started("A")
    runner.wait_started("B")
    time.sleep(0.3)
    assert "C" not in runner.started
    assert runner.peak == 2

    runner.release("A")
    runner.wait_started("C", timeout=10)
    with runner.lock:
        assert "B" in runner.started
        assert "B" not in runner.finished
        assert runner.active >= 2
    assert runner.peak == 2

    runner.release("B", "C")
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert all(state.get(task_id).status == TaskStatus.DONE for task_id in ("A", "B", "C"))


def test_none_memory_falls_back_to_max_parallel(tmp_path: Path, caplog):
    config, roadmap, state, worktrees, verifier, failures = init_repo(
        tmp_path, max_parallel=2, memory_per_agent_gb=2.0, min_free_memory_gb=2.0
    )
    runner = TrackingRunner()
    runner.gate("A", "B", "C")

    scheduler = _scheduler(
        tmp_path, config, state, worktrees, runner, verifier, failures,
        free_memory_gb=lambda: None,
    )

    with caplog.at_level(logging.WARNING, logger="harness.scheduler"):
        thread = threading.Thread(target=lambda: scheduler.run_sprint(roadmap.sprint("s1")))
        thread.start()
        runner.wait_started("A")
        runner.wait_started("B")
        time.sleep(0.2)
        assert runner.peak == 2
        assert "C" not in runner.started
        runner.release()
        thread.join(timeout=30)

    assert not thread.is_alive()
    assert sum("Free memory unavailable" in r.message for r in caplog.records) == 1
    assert all(state.get(task_id).status == TaskStatus.DONE for task_id in ("A", "B", "C"))


def test_current_memory_estimate_uses_config(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(
        tmp_path, ("A",), memory_per_agent_gb=2.25
    )
    scheduler = _scheduler(
        tmp_path, config, state, worktrees, TrackingRunner(), verifier, failures,
        free_memory_gb=lambda: 50.0,
    )
    assert scheduler.current_memory_estimate() == 2.25
