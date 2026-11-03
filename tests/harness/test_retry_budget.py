from __future__ import annotations

import subprocess
from pathlib import Path

from harness.cli import cmd_retry, cmd_unblock
from harness.config import HarnessConfig
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.models import TaskStatus
from harness.provider_runner import ProviderAgentRunner
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


def init_repo(root: Path):
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    write(root / ".gitignore", ".harness/\n")
    write(root / "AGENTS.md", "# test\n")
    # Cursor-only defaults: empty retry.sequence → one live Cursor implement call.
    write(root / "harness.yaml", """
execution:
  max_parallel_agents: 1
  default_max_attempts: 2
  default_timeout_minutes: 1
cursor:
  command: agent
  output_format: text
  models: {worker: cheap-model, reviewer: review-model}
retry:
  sequence: []
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths: {protected: [], review_required: []}
quota: {policy: stop, probe_interval_minutes: 60}
""")
    verify_out = file_exists_command("out.txt")
    write(root / "tasks/A.md", f"""---
id: A
stage: 1
model_class: worker
review: false
allowed_paths: [out.txt]
verification: [{yaml_quote(verify_out)}]
---
# A
""")
    write(root / "tasks/roadmap.yaml", f"""
project: test
sprints:
  s1:
    tasks:
      A: {{file: tasks/A.md, stage: 1, depends_on: []}}
    stage_verification:
      "1": [{yaml_quote(verify_out)}]
""")
    run(["git", "add", "."], root)
    run(["git", "commit", "-m", "initial"], root)
    config = HarnessConfig.load(root / "harness.yaml")
    roadmap = Roadmap(root, root / "tasks/roadmap.yaml")
    state = StateStore(root / ".harness/state.json")
    worktrees = WorktreeManager(root, root / ".harness")
    verifier = Verifier(config, root / ".harness/logs", worktrees)
    failures = FailureStore(root / ".harness/failures")
    return config, roadmap, state, worktrees, verifier, failures


class _FakeCodex:
    def availability(self, refresh: bool = False):
        return False, "disabled"

    def implement(self, *args, **kwargs):
        raise AssertionError("Codex should not implement in cursor-only budget tests")

    def review(self, *args, **kwargs):
        raise AssertionError("Codex should not review in cursor-only budget tests")


class _FailThenSucceedCursor:
    def __init__(self) -> None:
        self.implement_calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.implement_calls += 1
        if self.implement_calls == 1:
            return AgentResult(False, "", "first attempt failed", provider="cursor")
        (workspace / "out.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok", provider="cursor")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n", provider="cursor")


def _scheduler_with_cursor(
    root: Path,
    config: HarnessConfig,
    state: StateStore,
    worktrees: WorktreeManager,
    verifier: Verifier,
    failures: FailureStore,
    cursor: _FailThenSucceedCursor,
) -> Scheduler:
    runner = ProviderAgentRunner(config, state, _FakeCodex(), cursor)
    return Scheduler(root, config, state, worktrees, runner, verifier, failures=failures)


def _assert_blocked_after_cursor_budget(state: StateStore, cursor: _FailThenSucceedCursor) -> None:
    runtime = state.get("A")
    assert runtime.status == TaskStatus.BLOCKED
    assert runtime.provider_attempts == {"cursor": 1}
    assert cursor.implement_calls == 1
    assert runtime.worktree is not None


def test_retry_then_resume_calls_agent_after_cursor_budget(tmp_path: Path, capsys):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path)
    cursor = _FailThenSucceedCursor()
    scheduler = _scheduler_with_cursor(
        tmp_path, config, state, worktrees, verifier, failures, cursor
    )
    sprint = roadmap.sprint("s1")

    assert not scheduler.run_sprint(sprint)
    _assert_blocked_after_cursor_budget(state, cursor)
    worktree_before = state.get("A").worktree
    state.get("A").stall_counts = {"cursor:implement": 2}
    state.save()

    assert cmd_retry(state, "A") == 0
    out = capsys.readouterr().out
    assert "attempt, provider_attempts, stall_counts" in out
    runtime = state.get("A")
    assert runtime.status == TaskStatus.RETRY
    assert runtime.attempt == 0
    assert runtime.provider_attempts == {}
    assert runtime.stall_counts == {}
    assert runtime.worktree == worktree_before

    assert scheduler.run_sprint(sprint)
    assert cursor.implement_calls == 2
    assert state.get("A").status == TaskStatus.DONE


def test_unblock_then_resume_calls_agent_after_cursor_budget(tmp_path: Path, capsys):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path)
    cursor = _FailThenSucceedCursor()
    scheduler = _scheduler_with_cursor(
        tmp_path, config, state, worktrees, verifier, failures, cursor
    )
    sprint = roadmap.sprint("s1")

    assert not scheduler.run_sprint(sprint)
    _assert_blocked_after_cursor_budget(state, cursor)
    worktree_before = state.get("A").worktree
    state.get("A").stall_counts = {"cursor:implement": 2}
    state.save()

    assert cmd_unblock(state, "A") == 0
    out = capsys.readouterr().out
    assert "attempt, provider_attempts, stall_counts" in out
    runtime = state.get("A")
    assert runtime.status == TaskStatus.RETRY
    assert runtime.attempt == 0
    assert runtime.provider_attempts == {}
    assert runtime.stall_counts == {}
    assert runtime.worktree == worktree_before

    assert scheduler.run_sprint(sprint)
    assert cursor.implement_calls == 2
    assert state.get("A").status == TaskStatus.DONE
