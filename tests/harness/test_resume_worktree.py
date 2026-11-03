from __future__ import annotations

import logging
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
from tests.portable import file_exists_command, yaml_quote


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def worktree_records(root: Path) -> dict[Path, str]:
    output = run(["git", "worktree", "list", "--porcelain"], root)
    records: dict[Path, str] = {}
    path: Path | None = None
    for line in [*output.splitlines(), ""]:
        if not line:
            path = None
        elif line.startswith("worktree "):
            path = Path(line.removeprefix("worktree "))
        elif path is not None and line.startswith("branch refs/heads/"):
            records[path] = line.removeprefix("branch refs/heads/")
    return records


def init_repo(root: Path) -> tuple[HarnessConfig, Roadmap, StateStore, WorktreeManager, Verifier, str]:
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    write(root / ".gitignore", ".harness/\n")
    write(root / "AGENTS.md", "# test\n")
    write(
        root / "harness.yaml",
        """
execution:
  max_parallel_agents: 1
  default_max_attempts: 2
  default_timeout_minutes: 1
cursor:
  command: agent
  output_format: text
  models: {}
verification:
  global_task_commands: []
  stage_commands: []
paths:
  protected: []
  review_required: []
""",
    )
    verify = file_exists_command("out.txt")
    write(
        root / "tasks/A.md",
        f"""---
allowed_paths: [out.txt]
verification: [{yaml_quote(verify)}]
review: false
---
# A
""",
    )
    write(
        root / "tasks/roadmap.yaml",
        """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""",
    )
    run(["git", "add", "."], root)
    run(["git", "commit", "-m", "initial"], root)
    old_base = run(["git", "rev-parse", "HEAD"], root)
    config = HarnessConfig.load(root / "harness.yaml")
    roadmap = Roadmap(root, root / "tasks/roadmap.yaml")
    state = StateStore(root / ".harness/state.json")
    worktrees = WorktreeManager(root, root / ".harness")
    verifier = Verifier(config, root / ".harness/logs", worktrees)
    return config, roadmap, state, worktrees, verifier, old_base


class FakeRunner:
    def __init__(self) -> None:
        self.implement_calls = 0
        self.workspaces: list[Path] = []

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.implement_calls += 1
        self.workspaces.append(Path(workspace))
        (workspace / "out.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "fake success")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


def test_resume_replaces_unregistered_plain_folder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config, roadmap, state, worktrees, verifier, old_base = init_repo(tmp_path)
    write(tmp_path / "later.txt", "operator\n")
    run(["git", "add", "later.txt"], tmp_path)
    run(["git", "commit", "-m", "later"], tmp_path)
    newer_base = run(["git", "rev-parse", "HEAD"], tmp_path)

    stale = worktrees.worktree_root / "A"
    (stale / "graphify-out").mkdir(parents=True)
    (stale / "graphify-out" / "old.txt").write_text("stale\n", encoding="utf-8")
    runtime = state.get("A")
    runtime.worktree = str(stale)
    runtime.branch = "agent/A"
    runtime.base_ref = old_base
    runtime.status = TaskStatus.RETRY
    state.save()

    runner = FakeRunner()
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier)
    with caplog.at_level(logging.INFO, logger="harness.scheduler"):
        outcome = scheduler._run_task(roadmap.task("A"), newer_base)

    assert outcome.ok, state.get("A").last_error
    runtime = state.get("A")
    worktree = Path(runtime.worktree)
    assert worktree_records(tmp_path).get(worktree) == "agent/A"
    assert not (worktree / "graphify-out" / "old.txt").exists()
    assert runtime.base_ref == newer_base
    assert runner.implement_calls == 1
    assert runner.workspaces == [worktree]
    assert any(
        record.getMessage() == "worktree for A was not registered; created a fresh one"
        for record in caplog.records
    )


def test_resume_keeps_registered_worktree_and_old_base_ref(tmp_path: Path) -> None:
    config, roadmap, state, worktrees, verifier, old_base = init_repo(tmp_path)
    branch, worktree = worktrees.create("A", old_base)

    write(tmp_path / "later.txt", "operator\n")
    run(["git", "add", "later.txt"], tmp_path)
    run(["git", "commit", "-m", "later"], tmp_path)
    newer_base = run(["git", "rev-parse", "HEAD"], tmp_path)
    assert not (worktree / "later.txt").exists()

    runtime = state.get("A")
    runtime.worktree = str(worktree)
    runtime.branch = branch
    runtime.base_ref = old_base
    runtime.status = TaskStatus.RETRY
    state.save()

    runner = FakeRunner()
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier)
    outcome = scheduler._run_task(roadmap.task("A"), newer_base)

    assert outcome.ok, state.get("A").last_error
    runtime = state.get("A")
    assert Path(runtime.worktree) == worktree
    assert runtime.base_ref == old_base
    # Reuse kept the old checkout; a fresh create from newer_base would include later.txt.
    assert not (worktree / "later.txt").exists()
    assert runner.implement_calls == 1
    assert runner.workspaces == [worktree]
