from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from harness.agent_runner import AgentResult
from harness.config import HarnessConfig
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.models import TaskRuntime, TaskSpec, TaskStatus
from harness.provider_runner import (
    ProviderAgentRunner,
    apply_worktree_guard_block,
    is_worktree_guard_result,
    worktree_guard_agent_result,
    worktree_guard_error_message,
)
from harness.roadmap import Roadmap
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier
from tests.portable import file_exists_command, yaml_quote


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
    run(["git", "add", "."], root)
    run(["git", "commit", "-m", "initial"], root)


def _task(root: Path) -> TaskSpec:
    task_file = root / "task.md"
    task_file.write_text("# task\n", encoding="utf-8")
    return TaskSpec(id="T1", file=task_file, sprint="S1", stage=1, review=True, allowed_paths=["**"])


def _write_config(root: Path) -> HarnessConfig:
    payload = {
        "execution": {
            "max_parallel_agents": 1,
            "default_max_attempts": 2,
            "default_timeout_minutes": 1,
        },
        "cursor": {"enabled": True, "command": "agent", "models": {"worker": "composer-2.5"}},
        "providers": {"priority": ["codex", "cursor"]},
        "codex": {
            "enabled": True,
            "command": "codex",
            "implementation_sequence": [{"model": "gpt-5.6-sol", "reasoning_effort": "medium"}],
            "review_model": "gpt-5.6-sol",
            "planner_model": "gpt-5.6-sol",
        },
        "retry": {"sequence": ["worker"]},
        "verification": {"global_task_commands": [], "stage_commands": []},
        "paths": {"protected": [], "review_required": []},
    }
    path = root / "harness.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return HarnessConfig.load(path)


class _FakeCodex:
    def __init__(self) -> None:
        self.implement_calls = 0
        self.review_calls = 0
        self.plan_calls = 0

    def availability(self, refresh: bool = False):
        return True, "ok"

    def implement(self, *args, **kwargs):
        self.implement_calls += 1
        return AgentResult(True, "codex ok", provider="codex")

    def review(self, *args, **kwargs):
        self.review_calls += 1
        return AgentResult(True, "VERDICT: PASS\n", provider="codex")

    def plan_change(self, *args, **kwargs):
        self.plan_calls += 1
        return AgentResult(True, "CHANGE_KIND: NONE\n", provider="codex")


class _FakeCursor:
    def __init__(self) -> None:
        self.implement_calls = 0
        self.review_calls = 0
        self.plan_calls = 0

    def implement(self, *args, **kwargs):
        self.implement_calls += 1
        return AgentResult(True, "cursor ok", provider="cursor")

    def review(self, *args, **kwargs):
        self.review_calls += 1
        return AgentResult(True, "VERDICT: PASS\n", provider="cursor")

    def plan_change(self, *args, **kwargs):
        self.plan_calls += 1
        return AgentResult(True, "CHANGE_KIND: NONE\n", provider="cursor")


def _router(root: Path) -> tuple[ProviderAgentRunner, _FakeCodex, _FakeCursor, StateStore]:
    config = _write_config(root)
    state = StateStore(root / ".harness/state.json")
    codex = _FakeCodex()
    cursor = _FakeCursor()
    return ProviderAgentRunner(config, state, codex, cursor), codex, cursor, state


def test_registered_worktree_passes_guard(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    manager = WorktreeManager(repo, repo / ".harness")
    _, worktree = manager.create("T1", "HEAD")
    router, codex, cursor, _ = _router(repo)
    task = _task(repo)

    assert worktree_guard_agent_result(worktree) is None
    implement = router.implement(task, worktree, 1, 1, None)
    review = router.review(task, worktree, "HEAD", "ok", 1)

    assert implement.ok
    assert review.ok
    assert codex.implement_calls == 1
    assert cursor.implement_calls == 0
    assert codex.review_calls == 1
    assert cursor.review_calls == 0


def test_plain_folder_inside_repo_fails_guard(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    plain = repo / ".harness" / "worktrees" / "stale"
    plain.mkdir(parents=True)

    result = worktree_guard_agent_result(plain)

    assert result is not None
    assert not result.ok
    assert result.error == worktree_guard_error_message(plain.resolve(), repo.resolve())


def test_provider_implement_skips_agents_when_guard_fails(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    plain = repo / ".harness" / "worktrees" / "stale"
    plain.mkdir(parents=True)
    router, codex, cursor, state = _router(repo)
    task = _task(repo)

    result = router.implement(task, plain, 1, 1, None)
    runtime = state.get(task.id)

    assert is_worktree_guard_result(result)
    assert runtime.status == TaskStatus.BLOCKED
    assert runtime.last_error == result.error
    assert codex.implement_calls == 0
    assert cursor.implement_calls == 0


def test_apply_worktree_guard_block_refunds_implementing_attempt():
    runtime = TaskRuntime(status=TaskStatus.IMPLEMENTING, attempt=1)
    result = AgentResult(
        False, "", worktree_guard_error_message(Path("w"), None), provider="none",
    )

    assert apply_worktree_guard_block(runtime, result)
    assert runtime.status == TaskStatus.BLOCKED
    assert runtime.attempt == 0
    assert runtime.last_error == result.error


def test_git_command_failure_fails_guard_and_skips_agent(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    router, codex, cursor, _ = _router(repo)
    task = _task(repo)
    monkeypatch.setattr("harness.provider_runner._git_toplevel_reader", lambda _path: None)

    result = worktree_guard_agent_result(repo)
    implement = router.implement(task, repo, 1, 1, None)

    assert result is not None
    assert not result.ok
    assert result.error == worktree_guard_error_message(repo.resolve(), None)
    assert is_worktree_guard_result(implement)
    assert "top level is unknown" in (implement.error or "")
    assert codex.implement_calls == 0
    assert cursor.implement_calls == 0


def test_missing_git_executable_fails_guard(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "w"
    workspace.mkdir()

    def boom(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr("harness.provider_runner.subprocess.run", boom)

    result = worktree_guard_agent_result(workspace)

    assert result is not None
    assert not result.ok
    assert result.error == worktree_guard_error_message(workspace.resolve(), None)


def test_folder_outside_any_repo_fails_guard(tmp_path: Path):
    workspace = tmp_path / "w"
    workspace.mkdir()

    result = worktree_guard_agent_result(workspace)

    assert result is not None
    assert is_worktree_guard_result(result)


def test_worktree_guard_blocks_task_without_spending_attempt(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    config = _write_config(repo)
    verify_out = file_exists_command("out.txt")
    write(repo / "tasks/A.md", f"""---
allowed_paths: [out.txt]
verification: [{yaml_quote(verify_out)}]
review: false
---
# A
""")
    write(repo / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
""")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "tasks"], repo)
    roadmap = Roadmap(repo, repo / "tasks/roadmap.yaml")
    state = StateStore(repo / ".harness/state.json")
    worktrees = WorktreeManager(repo, repo / ".harness")
    verifier = Verifier(config, repo / ".harness/logs", worktrees)
    failures = FailureStore(repo / ".harness/failures")
    stale = worktrees.worktree_root / "A"
    stale.mkdir(parents=True)
    codex = _FakeCodex()
    cursor = _FakeCursor()
    runner = ProviderAgentRunner(config, state, codex, cursor)
    scheduler = Scheduler(repo, config, state, worktrees, runner, verifier, failures=failures)

    outcome = scheduler._run_task(roadmap.task("A"), worktrees.head_commit())
    runtime = state.get("A")

    assert not outcome.ok
    assert runtime.status == TaskStatus.BLOCKED
    assert runtime.attempt == 0
    assert runtime.last_error == worktree_guard_error_message(stale.resolve(), repo.resolve())
    assert "is not its own Git worktree" in (outcome.error or "")
    assert codex.implement_calls == 0
    assert cursor.implement_calls == 0
    assert not runner.has_implementation_budget(roadmap.task("A"))


def test_injected_reader_supports_non_git_workspaces_in_tests(tmp_path: Path, monkeypatch):
    workspace = tmp_path / "w"
    workspace.mkdir()
    monkeypatch.setattr(
        "harness.provider_runner._git_toplevel_reader",
        lambda _path: str(workspace.resolve()),
    )
    try:
        assert worktree_guard_agent_result(workspace) is None
    finally:
        monkeypatch.setattr("harness.provider_runner._git_toplevel_reader", None)


def test_review_and_plan_change_also_guarded(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    init_repo(repo)
    plain = repo / ".harness" / "worktrees" / "stale"
    plain.mkdir(parents=True)
    router, codex, cursor, _ = _router(repo)
    task = _task(repo)

    review = router.review(task, plain, "HEAD", "ok", 1)
    plan = router.plan_change(task, plain, "failure", 1, model_class="worker")

    assert is_worktree_guard_result(review)
    assert is_worktree_guard_result(plan)
    assert codex.review_calls == 0 and cursor.review_calls == 0
    assert codex.plan_calls == 0 and cursor.plan_calls == 0
