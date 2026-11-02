from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.config import HarnessConfig
from harness.git_worktree import WorktreeManager
from harness.models import TaskStatus
from harness.roadmap import Roadmap
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier
from tests.portable import fail_command, file_exists_command, yaml_quote


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class FakeRunner:
    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        out = workspace / "out"
        out.mkdir(exist_ok=True)
        (out / f"{task.id}.txt").write_text(task.id + "\n", encoding="utf-8")
        return AgentResult(True, "fake success")

    def review(self, task, workspace, base_ref, verification_summary, timeout_minutes, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


def _init_parallel_stage_repo(
    tmp_path: Path, *, stage_commands: list[str] | None = None,
) -> tuple[HarnessConfig, Roadmap, StateStore, WorktreeManager, Verifier]:
    run(["git", "init", "-b", "main"], tmp_path)
    run(["git", "config", "user.name", "Test"], tmp_path)
    run(["git", "config", "user.email", "test@example.com"], tmp_path)
    write(tmp_path / ".gitignore", ".harness/\n")
    write(tmp_path / "AGENTS.md", "# test\n")
    write(
        tmp_path / "harness.yaml",
        """
execution:
  max_parallel_agents: 2
  default_max_attempts: 1
  default_timeout_minutes: 1
  keep_successful_worktrees: false
cursor:
  command: agent
  output_format: text
  models: {}
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths:
  protected: []
  review_required: []
""",
    )
    for task_id in ["A", "B"]:
        verify = file_exists_command(f"out/{task_id}.txt")
        write(
            tmp_path / f"tasks/{task_id}.md",
            f"""---
allowed_paths: [out/{task_id}.txt]
verification: [{yaml_quote(verify)}]
review: false
---
# {task_id}
""",
        )
    stage_verification = ""
    if stage_commands is not None:
        quoted = ", ".join(yaml_quote(command) for command in stage_commands)
        stage_verification = f"""
    stage_verification:
      "1": [{quoted}]
"""
    write(
        tmp_path / "tasks/roadmap.yaml",
        f"""
project: test
sprints:
  s1:
    tasks:
      A: {{file: tasks/A.md, stage: 1, depends_on: []}}
      B: {{file: tasks/B.md, stage: 1, depends_on: []}}{stage_verification}
""",
    )
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    verifier = Verifier(config, tmp_path / ".harness/logs", worktrees)
    return config, roadmap, state, worktrees, verifier


def test_failed_worktree_removal_leaves_all_tasks_done_and_warns_with_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    config, roadmap, state, worktrees, verifier = _init_parallel_stage_repo(tmp_path, stage_commands=[])
    original_remove = worktrees.remove
    remove_calls = 0
    failing_path: Path | None = None

    def fail_first_remove(worktree_path: Path, branch: str, *, delete_branch: bool = True):
        nonlocal remove_calls, failing_path
        remove_calls += 1
        if remove_calls == 1:
            failing_path = worktree_path
            raise RuntimeError("synthetic cleanup failure")
        return original_remove(worktree_path, branch, delete_branch=delete_branch)

    worktrees.remove = fail_first_remove  # type: ignore[method-assign]
    scheduler = Scheduler(tmp_path, config, state, worktrees, FakeRunner(), verifier)

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("B").status == TaskStatus.DONE
    assert failing_path is not None
    log_text = (tmp_path / ".harness/logs/s1-stage-1-worktree-cleanup.log").read_text(encoding="utf-8")
    assert f"WARNING: failed to remove worktree {failing_path}" in log_text
    captured = capsys.readouterr()
    assert f"WARNING: failed to remove worktree {failing_path}" in captured.err


def test_failed_stage_barrier_leaves_no_task_merging(tmp_path: Path):
    config, roadmap, state, worktrees, verifier = _init_parallel_stage_repo(
        tmp_path, stage_commands=[fail_command()],
    )
    scheduler = Scheduler(tmp_path, config, state, worktrees, FakeRunner(), verifier)

    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    for task_id in ["A", "B"]:
        assert state.get(task_id).status != TaskStatus.MERGING
    assert state.get("A").status == TaskStatus.PASSED
    assert state.get("B").status == TaskStatus.PASSED
