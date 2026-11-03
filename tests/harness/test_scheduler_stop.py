from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

from harness.config import HarnessConfig
from harness.events import read_events
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.models import TaskStatus
from harness.roadmap import Roadmap
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier
from tests.portable import file_exists_command, python_command, yaml_quote


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _init_repo(
    root: Path,
    task_ids: tuple[str, ...] = ("A", "B"),
    *,
    max_parallel: int = 1,
    review: bool = False,
    release_commands: list[str] | None = None,
) -> tuple[HarnessConfig, Roadmap, StateStore, WorktreeManager, Verifier, FailureStore]:
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    write(root / ".gitignore", ".harness/\n")
    write(root / "AGENTS.md", "# test\n")
    release_block = ""
    if release_commands is not None:
        quoted = ", ".join(yaml_quote(cmd) for cmd in release_commands)
        release_block = f"""
release:
  commands: [{quoted}]
  timeout_minutes: 1
"""
    write(
        root / "harness.yaml",
        f"""
execution:
  max_parallel_agents: {max_parallel}
  default_max_attempts: 2
  default_timeout_minutes: 1
  watchdog_poll_seconds: 1
  keep_successful_worktrees: false
cursor:
  command: agent
  output_format: text
  models: {{}}
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths:
  protected: []
  review_required: []
{release_block}
""",
    )
    for task_id in task_ids:
        output = f"out-{task_id.lower()}.txt"
        check = file_exists_command(output)
        write(
            root / f"tasks/{task_id}.md",
            f"""---
allowed_paths: [{output}]
verification: [{yaml_quote(check)}]
review: {"true" if review else "false"}
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
      "1": []
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


class GatedRunner:
    def __init__(self) -> None:
        self.started: dict[str, threading.Event] = {}
        self.release: dict[str, threading.Event] = {}
        self.started_ids: list[str] = []
        self.lock = threading.Lock()

    def gate(self, *task_ids: str) -> None:
        for task_id in task_ids:
            self.started[task_id] = threading.Event()
            self.release[task_id] = threading.Event()

    def wait_started(self, task_id: str, timeout: float = 30.0) -> None:
        assert self.started[task_id].wait(timeout=timeout)

    def allow(self, *task_ids: str) -> None:
        for task_id in task_ids:
            self.release[task_id].set()

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        with self.lock:
            self.started_ids.append(task.id)
        gate = self.started.get(task.id)
        if gate is not None:
            gate.set()
        hold = self.release.get(task.id)
        if hold is not None:
            assert hold.wait(timeout=30)
        (workspace / f"out-{task.id.lower()}.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


class ReviewFailThenPassRunner:
    def __init__(self) -> None:
        self.review_calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        (workspace / f"out-{task.id.lower()}.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        self.review_calls += 1
        if self.review_calls == 1:
            return AgentResult(
                False,
                "VERDICT: FAIL\n- missing test\n- wrong path\n",
            )
        return AgentResult(True, "VERDICT: PASS\n")


class QuotaOnceRunner:
    def __init__(self) -> None:
        self.calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return AgentResult(
                False,
                "quota",
                "quota",
                quota_exhausted=True,
                provider="cursor",
            )
        (workspace / f"out-{task.id.lower()}.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok", provider="cursor")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n", provider="cursor")


def test_stop_request_stops_new_starts_and_resume_continues(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = _init_repo(
        tmp_path, ("A", "B"), max_parallel=1
    )
    runner = GatedRunner()
    runner.gate("A", "B")
    scheduler = Scheduler(
        tmp_path, config, state, worktrees, runner, verifier, failures=failures, roadmap=roadmap
    )
    sprint = roadmap.sprint("s1")
    result: list[bool] = []

    stop_noticed = threading.Event()
    original_stop = scheduler._stop_request_for_sprint

    def stop_probe(sprint_id: str) -> bool:
        found = original_stop(sprint_id)
        if found:
            stop_noticed.set()
        return found

    scheduler._stop_request_for_sprint = stop_probe  # type: ignore[method-assign]

    thread = threading.Thread(target=lambda: result.append(scheduler.run_sprint(sprint)))
    thread.start()
    runner.wait_started("A")

    stop_path = tmp_path / ".harness" / "stop-request"
    stop_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path.write_text(
        json.dumps({"sprint": "s1", "time": "2026-11-03T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    assert stop_noticed.wait(timeout=30)
    runner.allow("A")
    thread.join(timeout=120)
    assert not thread.is_alive()
    assert result == [False]
    assert "B" not in runner.started_ids
    assert state.get("A").status == TaskStatus.PASSED
    assert state.get("B").status == TaskStatus.PENDING
    assert not stop_path.exists()
    assert failures.get("s1")["kind"] == "STOP_REQUEST"
    texts = [item["text"] for item in read_events(tmp_path, "s1", limit=50)]
    assert "stop requested" in texts

    runner.allow("B")
    assert scheduler.run_sprint(sprint)
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("B").status == TaskStatus.DONE


def test_stop_request_consumed_when_last_inflight_tasks_finish(tmp_path: Path):
    """Stop while the only in-flight task runs; after it PASSes, still pause and clear the file."""
    config, roadmap, state, worktrees, verifier, failures = _init_repo(
        tmp_path, ("A",), max_parallel=1
    )
    runner = GatedRunner()
    runner.gate("A")
    scheduler = Scheduler(
        tmp_path, config, state, worktrees, runner, verifier, failures=failures, roadmap=roadmap
    )
    sprint = roadmap.sprint("s1")
    result: list[bool] = []

    stop_noticed = threading.Event()
    original_stop = scheduler._stop_request_for_sprint

    def stop_probe(sprint_id: str) -> bool:
        found = original_stop(sprint_id)
        if found:
            stop_noticed.set()
        return found

    scheduler._stop_request_for_sprint = stop_probe  # type: ignore[method-assign]

    thread = threading.Thread(target=lambda: result.append(scheduler.run_sprint(sprint)))
    thread.start()
    runner.wait_started("A")

    stop_path = tmp_path / ".harness" / "stop-request"
    stop_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path.write_text(
        json.dumps({"sprint": "s1", "time": "2026-11-03T00:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )
    assert stop_noticed.wait(timeout=30)
    runner.allow("A")
    thread.join(timeout=120)
    assert not thread.is_alive()
    assert result == [False]
    assert state.get("A").status == TaskStatus.PASSED
    assert not stop_path.exists()
    assert failures.get("s1")["kind"] == "STOP_REQUEST"
    texts = [item["text"] for item in read_events(tmp_path, "s1", limit=50)]
    assert "stop requested" in texts

    assert scheduler.run_sprint(sprint)
    assert state.get("A").status == TaskStatus.DONE


def test_scheduler_emits_status_review_wait_merge_and_release_events(tmp_path: Path):
    release_cmd = python_command("import pathlib; pathlib.Path('released.txt').write_text('ok')")
    config, roadmap, state, worktrees, verifier, failures = _init_repo(
        tmp_path,
        ("A",),
        max_parallel=1,
        review=True,
        release_commands=[release_cmd],
    )
    runner = ReviewFailThenPassRunner()
    scheduler = Scheduler(
        tmp_path, config, state, worktrees, runner, verifier, failures=failures, roadmap=roadmap
    )
    sprint = roadmap.sprint("s1")
    assert scheduler.run_sprint(sprint)
    assert state.get("A").status == TaskStatus.DONE
    assert state.get_meta("s1.release.status") == "done"

    texts = [item["text"] for item in read_events(tmp_path, "s1", limit=100)]
    assert "A IMPLEMENTING" in texts
    assert "A CHECKING" in texts
    assert "A REVIEWING" in texts
    assert "A review FAIL (2 findings)" in texts
    assert "A RETRY" in texts
    assert "A review PASS (0 findings)" in texts
    assert "A PASSED" in texts
    assert "stage 1 merge started" in texts
    assert "stage 1 merged" in texts
    assert "A MERGING" in texts
    assert "A DONE" in texts
    assert "release done" in texts
    all_events = read_events(tmp_path, "s1", limit=1000)
    times = [item["time"] for item in all_events]
    assert times == sorted(times, reverse=True)


def test_wait_event_includes_cause_and_provider(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = _init_repo(
        tmp_path, ("A",), max_parallel=1
    )
    runner = QuotaOnceRunner()
    scheduler = Scheduler(
        tmp_path, config, state, worktrees, runner, verifier, failures=failures, roadmap=roadmap
    )
    sprint = roadmap.sprint("s1")
    assert not scheduler.run_sprint(sprint)
    assert state.get("A").status == TaskStatus.WAITING_QUOTA
    texts = [item["text"] for item in read_events(tmp_path, "s1", limit=20)]
    assert "A WAITING_QUOTA (cursor)" in texts

    assert scheduler.run_sprint(sprint)
    assert state.get("A").status == TaskStatus.DONE


def test_task_added_event(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = _init_repo(
        tmp_path, ("A",), max_parallel=1
    )
    write(tmp_path / ".gitignore", ".harness/\nextra/\n")
    run(["git", "add", ".gitignore"], tmp_path)
    run(["git", "commit", "-m", "ignore extra"], tmp_path)
    add_check = file_exists_command("out-c.txt")
    write(
        tmp_path / "extra/C.md",
        f"""---
id: C
allowed_paths: [out-c.txt]
verification: [{yaml_quote(add_check)}]
review: false
depends_on: []
---
# C
""",
    )

    class SimpleRunner:
        def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
            (workspace / f"out-{task.id.lower()}.txt").write_text("ok\n", encoding="utf-8")
            return AgentResult(True, "ok")

        def review(self, *args, **kwargs):
            return AgentResult(True, "VERDICT: PASS\n")

    scheduler = Scheduler(
        tmp_path,
        config,
        state,
        worktrees,
        SimpleRunner(),
        verifier,
        failures=failures,
        roadmap=roadmap,
        roadmap_path=tmp_path / "tasks/roadmap.yaml",
    )
    scheduler.task_queue.enqueue(tmp_path / "extra/C.md", "s1", roadmap, stage_choice="next")
    sprint = roadmap.sprint("s1")
    assert scheduler.run_sprint(sprint)
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("C").status == TaskStatus.DONE
    texts = [item["text"] for item in read_events(tmp_path, "s1", limit=50)]
    assert "added C" in texts
