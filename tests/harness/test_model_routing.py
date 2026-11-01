from __future__ import annotations

import subprocess
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


def setup_repo(root: Path):
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    write(root / ".gitignore", ".harness/\n")
    write(root / "AGENTS.md", "# test\n")
    write(root / "harness.yaml", """
execution:
  max_parallel_agents: 1
  default_max_attempts: 2
  default_timeout_minutes: 1
cursor:
  command: agent
  output_format: text
  models:
    worker: composer-2.5
    hard_worker: cursor-grok-4.6-high
    reviewer: cursor-grok-4.6-high
    escalation: claude-opus-5-thinking-high
retry:
  sequence: [worker, worker, hard_worker, escalation]
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths: {protected: [], review_required: []}
quota: {policy: stop, probe_interval_minutes: 60}
plan_repair: {enabled: true, planner_model_class: escalation, timeout_minutes: 1}
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


class EscalatingRunner:
    def __init__(self):
        self.calls: list[str | None] = []

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        model_class = kwargs.get("model_class_override")
        self.calls.append(model_class)
        if len(self.calls) < 4:
            return AgentResult(False, f"failed {model_class}", f"failed {model_class}")
        (workspace / "out.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


class AlwaysFailRunner:
    def __init__(self):
        self.calls: list[str | None] = []

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.calls.append(kwargs.get("model_class_override"))
        return AgentResult(False, "still broken", "still broken")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


def test_automatic_attempts_follow_composer_composer_grok_opus(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = setup_repo(tmp_path)
    runner = EscalatingRunner()
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert runner.calls == ["worker", "worker", "hard_worker", "escalation"]
    assert state.get("A").attempt == 4
    assert state.get("A").status == TaskStatus.DONE


def test_focused_expensive_override_is_one_shot(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = setup_repo(tmp_path)
    runner = AlwaysFailRunner()
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)
    task = roadmap.task("A")
    base = worktrees.head_commit()

    outcome = scheduler._run_task(task, base, model_class_override="escalation")

    assert not outcome.ok
    assert runner.calls == ["escalation"]
    assert state.get("A").attempt == 1
    assert state.get("A").status == TaskStatus.BLOCKED
