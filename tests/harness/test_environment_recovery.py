from __future__ import annotations

import subprocess
from pathlib import Path

from harness.config import HarnessConfig
from harness.environment import EnvironmentManager
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.models import TaskSpec, TaskStatus
from harness.roadmap import Roadmap
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier
from tests.portable import file_exists_command, make_python_script, yaml_quote


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def init_repo(root: Path) -> None:
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    write(root / ".gitignore", ".harness/\n")
    write(root / "AGENTS.md", "# test\n")


class SelectiveRunner:
    def __init__(self, fail_ids: set[str] | None = None):
        self.fail_ids = fail_ids or set()
        self.calls: dict[str, int] = {}
        self.overrides: list[tuple[str, str | None]] = []

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.calls[task.id] = self.calls.get(task.id, 0) + 1
        self.overrides.append((task.id, kwargs.get("model_class_override")))
        if task.id in self.fail_ids:
            return AgentResult(False, "synthetic fail", "synthetic fail")
        out = workspace / "out"
        out.mkdir(exist_ok=True)
        (out / f"{task.id}.txt").write_text(task.id + "\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, task, workspace, base_ref, verification_summary, timeout_minutes, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


def build_two_task_repo(root: Path, *, stage_command: str | None = None) -> tuple[HarnessConfig, Roadmap, StateStore, WorktreeManager, Verifier]:
    write(root / "harness.yaml", """
execution:
  max_parallel_agents: 2
  default_max_attempts: 1
  default_timeout_minutes: 1
cursor:
  command: agent
  output_format: text
  models:
    worker: null
    escalation: null
    reviewer: null
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths: {protected: [], review_required: []}
""")
    for tid in ["A", "B"]:
        verify = file_exists_command(f"out/{tid}.txt")
        write(root / f"tasks/{tid}.md", f"""---
allowed_paths: [out/{tid}.txt]
verification: [{yaml_quote(verify)}]
review: false
---
# {tid}
""")
    stage = (f'\n    stage_verification:\n      "1": [{yaml_quote(stage_command)}]\n' if stage_command else "")
    write(root / "tasks/roadmap.yaml", f"""
project: test
sprints:
  s1:
    tasks:
      A: {{file: tasks/A.md, stage: 1, depends_on: []}}
      B: {{file: tasks/B.md, stage: 1, depends_on: []}}{stage}
""")
    run(["git", "add", "."], root)
    run(["git", "commit", "-m", "initial"], root)
    config = HarnessConfig.load(root / "harness.yaml")
    roadmap = Roadmap(root, root / "tasks/roadmap.yaml")
    state = StateStore(root / ".harness/state.json")
    worktrees = WorktreeManager(root, root / ".harness")
    verifier = Verifier(config, root / ".harness/logs", worktrees)
    return config, roadmap, state, worktrees, verifier


def test_blocked_task_can_be_rerun_alone_then_resume_without_rerunning_sibling(tmp_path: Path):
    init_repo(tmp_path)
    config, roadmap, state, worktrees, verifier = build_two_task_repo(tmp_path)
    runner = SelectiveRunner({"A"})
    failures = FailureStore(tmp_path / ".harness/failures")
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)

    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.BLOCKED
    assert state.get("B").status == TaskStatus.VERIFIED
    record = failures.get("s1")
    assert record is not None
    assert record["kind"] == "TASK_BLOCKED"
    assert record["task_ids"] == ["A"]

    # Focused expensive-model retry: keep B's verified work, retry only A.
    runner.fail_ids.clear()
    a = state.get("A")
    a.status = TaskStatus.RETRY
    a.attempt = 0
    state.save()
    assert scheduler._run_task(
        roadmap.task("A"), a.base_ref or worktrees.head_commit(), model_class_override="escalation"
    ).ok
    assert state.get("A").status == TaskStatus.VERIFIED
    assert runner.overrides[-1] == ("A", "escalation")
    assert runner.calls == {"A": 2, "B": 1}

    # Normal resume integrates already-verified A+B; no agent is rerun.
    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("B").status == TaskStatus.DONE
    assert runner.calls == {"A": 2, "B": 1}


def test_failed_stage_barrier_is_rerunnable_without_rerunning_agents(tmp_path: Path):
    init_repo(tmp_path)
    marker = tmp_path.parent / f"{tmp_path.name}-stage-ok"
    config, roadmap, state, worktrees, verifier = build_two_task_repo(
        tmp_path, stage_command=file_exists_command(marker)
    )
    runner = SelectiveRunner()
    failures = FailureStore(tmp_path / ".harness/failures")
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)

    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.VERIFIED
    assert state.get("B").status == TaskStatus.VERIFIED
    record = failures.get("s1")
    assert record is not None and record["kind"] == "STAGE_BARRIER"
    assert runner.calls == {"A": 1, "B": 1}

    marker.write_text("ok\n", encoding="utf-8")
    try:
        assert scheduler.rerun_stage_barrier(roadmap.sprint("s1"), 1)
    finally:
        marker.unlink(missing_ok=True)
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("B").status == TaskStatus.DONE
    assert runner.calls == {"A": 1, "B": 1}
    assert failures.get("s1") is None


def test_docker_environment_uses_isolated_compose_names_and_lifecycle(tmp_path: Path):
    log = tmp_path / "docker.log"
    fake = make_python_script(tmp_path / "fake-docker.py", """import os
import sys
with open(os.environ['FAKE_DOCKER_LOG'], 'a', encoding='utf-8') as fh:
    fh.write(os.environ.get('COMPOSE_PROJECT_NAME', '') + '|' + ' '.join(sys.argv[1:]) + '\\n')
""")
    write(tmp_path / "harness.yaml", f"""
cursor: {{models: {{worker: null}}}}
environment:
  docker_enabled: true
  compose_command: {yaml_quote(fake)}
  compose_files: [docker-compose.yml]
  project_name_prefix: demo
  validate_compose: true
  up_before_agent: true
  teardown_on_success: true
  teardown_on_blocked: true
  remove_volumes: true
  env:
    FAKE_DOCKER_LOG: {yaml_quote(log)}
""")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    manager = EnvironmentManager(tmp_path, config, tmp_path / ".harness/logs")
    a = TaskSpec(id="S1-T1", file=Path("x"), sprint="s1", stage=1, environment="docker")
    b = TaskSpec(id="S1-T2", file=Path("x"), sprint="s1", stage=1, environment="docker")

    assert manager.task_env(a, tmp_path)["COMPOSE_PROJECT_NAME"] != manager.task_env(b, tmp_path)["COMPOSE_PROJECT_NAME"]
    assert manager.prepare_task(a, tmp_path).ok
    assert manager.teardown_task(a, tmp_path).ok
    lines = log.read_text(encoding="utf-8").splitlines()
    assert any("demo-s1-t1|-f docker-compose.yml config --quiet" in line for line in lines)
    assert any("demo-s1-t1|-f docker-compose.yml up -d --remove-orphans" in line for line in lines)
    assert any("demo-s1-t1|-f docker-compose.yml down --remove-orphans --volumes" in line for line in lines)
