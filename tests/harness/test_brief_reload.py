from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from harness.agent_runner import AgentResult
from harness.config import HarnessConfig
from harness.git_worktree import WorktreeManager
from harness.models import TaskStatus
from harness.roadmap import Roadmap
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier


def _run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=True)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _brief(allowed_paths: str) -> str:
    return f"""---
id: A
stage: 1
depends_on: []
allowed_paths: [{allowed_paths}]
verification: []
timeout_minutes: 1
review: false
allow_protected: false
context: [spec]
---
# A
"""


def _scheduler(tmp_path: Path, runner) -> tuple[Scheduler, Roadmap, StateStore]:
    _run(["git", "init", "-b", "main"], tmp_path)
    _run(["git", "config", "user.name", "Test"], tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], tmp_path)
    _write(tmp_path / ".gitignore", ".harness/\n")
    _write(
        tmp_path / "harness.yaml",
        """
execution:
  default_max_attempts: 2
  default_timeout_minutes: 1
cursor: {command: agent, output_format: text, models: {}}
verification: {global_task_commands: [], stage_commands: []}
paths: {protected: [], review_required: []}
""",
    )
    _write(tmp_path / "tasks/A.md", _brief("first.txt"))
    _write(
        tmp_path / "tasks/roadmap.yaml",
        """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
""",
    )
    _run(["git", "add", "."], tmp_path)
    _run(["git", "commit", "-m", "initial"], tmp_path)

    config = HarnessConfig.load(tmp_path / "harness.yaml")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    verifier = Verifier(config, tmp_path / ".harness/logs", worktrees)
    return Scheduler(tmp_path, config, state, worktrees, runner, verifier), roadmap, state


class ReloadingRunner:
    def __init__(self, root: Path, replacement: str):
        self.root = root
        self.replacement = replacement
        self.calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.calls += 1
        (workspace / "second.txt").write_text("created\n", encoding="utf-8")
        if self.calls == 1:
            (self.root / "tasks/A.md").write_text(self.replacement, encoding="utf-8")
        return AgentResult(True, "STATUS: implementation finished\nOPEN_ISSUES: none")

    def review(self, *args, **kwargs):
        raise AssertionError("review is disabled")


def test_next_attempt_uses_reloaded_allowed_paths(tmp_path: Path, caplog) -> None:
    runner = ReloadingRunner(tmp_path, _brief("first.txt, second.txt"))
    scheduler, roadmap, state = _scheduler(tmp_path, runner)
    task = roadmap.sprint("s1").tasks["A"]

    with caplog.at_level(logging.INFO, logger="harness.scheduler"):
        outcome = scheduler._run_task(task, "HEAD")

    assert outcome.ok
    assert runner.calls == 2
    assert task.allowed_paths == ["first.txt", "second.txt"]
    assert state.get("A").status == TaskStatus.PASSED
    assert "changed fields: allowed_paths" in caplog.text


def test_blocked_report_with_failed_verification_stops_retrying(tmp_path: Path) -> None:
    class BlockedRunner(ReloadingRunner):
        def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
            self.calls += 1
            (workspace / "second.txt").write_text("created\n", encoding="utf-8")
            return AgentResult(
                True,
                "STATUS: blocked\nCHANGED_FILES: second.txt\n"
                "TEST_RESULTS: not run\nOPEN_ISSUES: Need second.txt in allowed_paths",
            )

    runner = BlockedRunner(tmp_path, _brief("first.txt"))
    scheduler, roadmap, state = _scheduler(tmp_path, runner)

    outcome = scheduler._run_task(roadmap.sprint("s1").tasks["A"], "HEAD")

    assert not outcome.ok
    assert runner.calls == 1
    assert state.get("A").status == TaskStatus.BLOCKED
    assert state.get("A").last_error == "Need second.txt in allowed_paths"


@pytest.mark.parametrize(
    "replacement",
    [
        "---\nallowed_paths: [broken\n",
        "# Brief without frontmatter\n",
        "---\n---\n# Brief with empty frontmatter\n",
    ],
)
def test_broken_brief_keeps_previous_spec_and_warns_once(
    tmp_path: Path, caplog, replacement: str
) -> None:
    runner = ReloadingRunner(tmp_path, replacement)
    scheduler, roadmap, state = _scheduler(tmp_path, runner)
    task = roadmap.sprint("s1").tasks["A"]

    with caplog.at_level(logging.WARNING, logger="harness.scheduler"):
        outcome = scheduler._run_task(task, "HEAD")

    assert not outcome.ok
    assert runner.calls == 2
    assert task.allowed_paths == ["first.txt"]
    assert state.get("A").status == TaskStatus.BLOCKED
    warnings = [record for record in caplog.records if "could not reload brief" in record.message]
    assert len(warnings) == 1
