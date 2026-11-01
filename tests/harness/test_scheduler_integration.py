from __future__ import annotations

import subprocess
from pathlib import Path

from harness.config import HarnessConfig
from harness.git_worktree import WorktreeManager
from harness.models import TaskSpec, TaskStatus
from harness.roadmap import Roadmap
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier
from tests.portable import fail_command, file_exists_command, write_text_command, yaml_quote


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


class FakeRunner:
    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        out = workspace / "out"
        out.mkdir(exist_ok=True)
        (out / f"{task.id}.txt").write_text(task.id + "\n", encoding="utf-8")
        return AgentResult(True, "fake success")

    def review(self, task, workspace, base_ref, verification_summary, timeout_minutes, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_parallel_stage_then_dependency_stage_merges(tmp_path: Path):
    run(["git", "init", "-b", "main"], tmp_path)
    run(["git", "config", "user.name", "Test"], tmp_path)
    run(["git", "config", "user.email", "test@example.com"], tmp_path)

    write(tmp_path / ".gitignore", ".harness/\n")
    write(tmp_path / "AGENTS.md", "# test\n")
    write(tmp_path / "harness.yaml", """
execution:
  max_parallel_agents: 2
  default_max_attempts: 2
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
""")
    for task_id in ["A", "B", "C"]:
        verify = file_exists_command(f"out/{task_id}.txt")
        write(tmp_path / f"tasks/{task_id}.md", f"""---
allowed_paths: [out/{task_id}.txt]
verification: [{yaml_quote(verify)}]
review: false
---
# {task_id}
""")
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
      B: {file: tasks/B.md, stage: 1, depends_on: []}
      C: {file: tasks/C.md, stage: 2, depends_on: [A, B]}
    stage_verification:
      "1": []
      "2": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    config = HarnessConfig.load(tmp_path / "harness.yaml")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    verifier = Verifier(config, tmp_path / ".harness/logs", worktrees)
    scheduler = Scheduler(tmp_path, config, state, worktrees, FakeRunner(), verifier)

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("B").status == TaskStatus.DONE
    assert state.get("C").status == TaskStatus.DONE
    assert (tmp_path / "out/A.txt").exists()
    assert (tmp_path / "out/B.txt").exists()
    assert (tmp_path / "out/C.txt").exists()


class SetupCheckingRunner(FakeRunner):
    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        assert (workspace / ".setup-ready").read_text(encoding="utf-8").strip() == "ready"
        return super().implement(task, workspace, timeout_minutes, attempt, previous_failure)


class ReviewCountingRunner(FakeRunner):
    def __init__(self):
        self.review_calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        (workspace / "ARCHITECTURE.md").write_text("changed by task\n", encoding="utf-8")
        return AgentResult(True, "fake success")

    def review(self, task, workspace, base_ref, verification_summary, timeout_minutes, **kwargs):
        self.review_calls += 1
        return AgentResult(True, "VERDICT: PASS\n")


def _init_repo(tmp_path: Path) -> None:
    run(["git", "init", "-b", "main"], tmp_path)
    run(["git", "config", "user.name", "Test"], tmp_path)
    run(["git", "config", "user.email", "test@example.com"], tmp_path)


def test_worktree_setup_runs_before_agent(tmp_path: Path):
    _init_repo(tmp_path)
    write(tmp_path / ".gitignore", ".harness/\n.setup-ready\n")
    write(tmp_path / "AGENTS.md", "# test\n")
    setup_command = write_text_command(".setup-ready", "ready\n")
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", f"""
execution:
  max_parallel_agents: 1
  default_max_attempts: 1
  default_timeout_minutes: 1
worktree:
  setup_commands: [{yaml_quote(setup_command)}]
  setup_timeout_minutes: 1
cursor: {{command: agent, output_format: text, models: {{}}}}
verification: {{global_task_commands: [git diff --check], stage_commands: []}}
paths: {{protected: [], review_required: []}}
""")
    write(tmp_path / "tasks/A.md", f"""---
allowed_paths: [out/A.txt]
verification: [{yaml_quote(verify_a)}]
review: false
---
# A
""")
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    config = HarnessConfig.load(tmp_path / "harness.yaml")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    verifier = Verifier(config, tmp_path / ".harness/logs", worktrees)
    scheduler = Scheduler(tmp_path, config, state, worktrees, SetupCheckingRunner(), verifier)

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.DONE
    assert (tmp_path / ".harness/logs/A-worktree-setup.log").exists()

def test_worktree_setup_timeout_decodes_captured_bytes_and_blocks_cleanly(tmp_path: Path, monkeypatch):
    write(tmp_path / "harness.yaml", """
worktree:
  setup_commands: [fake-setup]
  setup_timeout_minutes: 1
cursor: {command: agent, output_format: text, models: {}}
verification: {global_task_commands: [], stage_commands: []}
paths: {protected: [], review_required: []}
""")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    verifier = Verifier(config, tmp_path / ".harness/logs", worktrees)
    scheduler = Scheduler(tmp_path, config, state, worktrees, FakeRunner(), verifier)
    task = TaskSpec(id="A", file=Path("tasks/A.md"), sprint="s1", stage=1)

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(
            cmd="fake-setup",
            timeout=60,
            output=b"partial stdout \xff",
            stderr=b"partial stderr \xfe",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    ok, error = scheduler._setup_worktree(task, tmp_path, {})

    assert not ok
    assert error == "Worktree setup timed out: fake-setup"
    log = (tmp_path / ".harness/logs/A-worktree-setup.log").read_text(encoding="utf-8")
    assert "TIMEOUT" in log
    assert "partial stdout �" in log
    assert "partial stderr �" in log


def test_review_required_forces_review_even_when_task_disables_it(tmp_path: Path):
    _init_repo(tmp_path)
    write(tmp_path / ".gitignore", ".harness/\n")
    write(tmp_path / "ARCHITECTURE.md", "original\n")
    write(tmp_path / "harness.yaml", """
execution:
  max_parallel_agents: 1
  default_max_attempts: 1
  default_timeout_minutes: 1
worktree: {}
cursor: {command: agent, output_format: text, models: {}}
verification: {global_task_commands: [git diff --check], stage_commands: []}
paths:
  protected: []
  review_required: [ARCHITECTURE.md]
""")
    write(tmp_path / "tasks/A.md", """---
allowed_paths: [ARCHITECTURE.md]
review: false
---
# A
""")
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    config = HarnessConfig.load(tmp_path / "harness.yaml")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    verifier = Verifier(config, tmp_path / ".harness/logs", worktrees)
    runner = ReviewCountingRunner()
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier)

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert runner.review_calls == 1


def test_failed_stage_verification_rolls_back_all_stage_merges(tmp_path: Path):
    _init_repo(tmp_path)
    write(tmp_path / ".gitignore", ".harness/\n")
    write(tmp_path / "AGENTS.md", "# test\n")
    write(tmp_path / "harness.yaml", """
execution:
  max_parallel_agents: 2
  default_max_attempts: 1
  default_timeout_minutes: 1
  keep_successful_worktrees: false
worktree: {}
cursor: {command: agent, output_format: text, models: {}}
verification: {global_task_commands: [git diff --check], stage_commands: []}
paths: {protected: [], review_required: []}
""")
    for task_id in ["A", "B"]:
        verify = file_exists_command(f"out/{task_id}.txt")
        write(tmp_path / f"tasks/{task_id}.md", f"""---
allowed_paths: [out/{task_id}.txt]
verification: [{yaml_quote(verify)}]
review: false
---
# {task_id}
""")
    failing_stage = fail_command()
    write(tmp_path / "tasks/roadmap.yaml", f"""
project: test
sprints:
  s1:
    tasks:
      A: {{file: tasks/A.md, stage: 1, depends_on: []}}
      B: {{file: tasks/B.md, stage: 1, depends_on: []}}
    stage_verification:
      "1": [{yaml_quote(failing_stage)}]
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)
    initial_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, text=True, capture_output=True
    ).stdout.strip()

    config = HarnessConfig.load(tmp_path / "harness.yaml")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    verifier = Verifier(config, tmp_path / ".harness/logs", worktrees)
    scheduler = Scheduler(tmp_path, config, state, worktrees, FakeRunner(), verifier)

    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    current_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, text=True, capture_output=True
    ).stdout.strip()
    assert current_head == initial_head
    assert not (tmp_path / "out/A.txt").exists()
    assert not (tmp_path / "out/B.txt").exists()
    assert state.get("A").status == TaskStatus.VERIFIED
    assert state.get("B").status == TaskStatus.VERIFIED
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=tmp_path, check=True, text=True, capture_output=True
    ).stdout.strip()
    assert status == ""


class FailingSecondMergeWorktreeManager(WorktreeManager):
    def __init__(self, repo_root: Path, runtime_root: Path):
        super().__init__(repo_root, runtime_root)
        self.merge_count = 0

    def merge_branch(self, branch: str, task_id: str) -> None:
        self.merge_count += 1
        if self.merge_count == 2:
            raise RuntimeError("synthetic second-merge failure")
        return super().merge_branch(branch, task_id)


def test_partial_merge_failure_rolls_back_first_merge(tmp_path: Path):
    _init_repo(tmp_path)
    write(tmp_path / ".gitignore", ".harness/\n")
    write(tmp_path / "AGENTS.md", "# test\n")
    write(tmp_path / "harness.yaml", """
execution:
  max_parallel_agents: 2
  default_max_attempts: 1
  default_timeout_minutes: 1
worktree: {}
cursor: {command: agent, output_format: text, models: {}}
verification: {global_task_commands: [git diff --check], stage_commands: []}
paths: {protected: [], review_required: []}
""")
    for task_id in ["A", "B"]:
        verify = file_exists_command(f"out/{task_id}.txt")
        write(tmp_path / f"tasks/{task_id}.md", f"""---
allowed_paths: [out/{task_id}.txt]
verification: [{yaml_quote(verify)}]
review: false
---
# {task_id}
""")
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
      B: {file: tasks/B.md, stage: 1, depends_on: []}
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)
    initial_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, check=True, text=True, capture_output=True
    ).stdout.strip()

    config = HarnessConfig.load(tmp_path / "harness.yaml")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = FailingSecondMergeWorktreeManager(tmp_path, tmp_path / ".harness")
    verifier = Verifier(config, tmp_path / ".harness/logs", worktrees)
    scheduler = Scheduler(tmp_path, config, state, worktrees, FakeRunner(), verifier)

    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert worktrees.merge_count == 2
    assert worktrees.head_commit() == initial_head
    assert not (tmp_path / "out/A.txt").exists()
    assert not (tmp_path / "out/B.txt").exists()
    assert state.get("A").status == TaskStatus.VERIFIED
    assert state.get("B").status == TaskStatus.VERIFIED


class FailingOnceRunner(FakeRunner):
    def __init__(self):
        self.calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.calls += 1
        if self.calls == 1:
            return AgentResult(False, "", "first attempt fails")
        return super().implement(task, workspace, timeout_minutes, attempt, previous_failure)


def test_resumed_worktree_is_diffed_against_its_own_base_not_a_newer_head(tmp_path: Path):
    _init_repo(tmp_path)
    write(tmp_path / ".gitignore", ".harness/\n")
    write(tmp_path / "AGENTS.md", "# test\n")
    write(tmp_path / "settings.txt", "parallel: 4\n")
    write(tmp_path / "harness.yaml", """
execution:
  max_parallel_agents: 1
  default_max_attempts: 1
  default_timeout_minutes: 1
cursor:
  command: agent
  output_format: text
  models: {}
verification:
  global_task_commands: []
  stage_commands: []
paths:
  protected: [settings.txt]
  review_required: []
""")
    verify = file_exists_command("out/A.txt")
    write(tmp_path / "tasks/A.md", f"""---
allowed_paths: [out/A.txt]
verification: [{yaml_quote(verify)}]
review: false
---
# A
""")
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)
    first_base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True, capture_output=True, check=True).stdout.strip()

    config = HarnessConfig.load(tmp_path / "harness.yaml")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    verifier = Verifier(config, tmp_path / ".harness/logs", worktrees)
    runner = FailingOnceRunner()
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier)
    task = roadmap.sprint("s1").tasks["A"]

    assert not scheduler._run_task(task, first_base).ok
    assert state.get("A").base_ref == first_base

    # The operator commits a protected file between the blocked attempt and the retry.
    write(tmp_path / "settings.txt", "parallel: 2\n")
    run(["git", "commit", "-am", "operator change"], tmp_path)
    newer_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True, capture_output=True, check=True).stdout.strip()

    runtime = state.get("A")
    runtime.status = TaskStatus.RETRY
    runtime.attempt = 0
    state.save()
    outcome = scheduler._run_task(task, newer_head)

    assert outcome.ok, state.get("A").last_error
    assert "settings.txt" not in (state.get("A").last_error or "")
