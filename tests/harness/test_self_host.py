from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import harness.cli as cli
from harness.config import HarnessConfig, SelfHostingConfig
from harness.events import read_events
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.models import TaskStatus
from harness.roadmap import Roadmap
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.self_host import (
    ensure_frozen_copy,
    frozen_copy_path,
    prune_frozen_copies,
    resume_command,
    run_resume_with_frozen_copy,
    self_root,
    short_commit,
)
from harness.state import StateStore
from harness.verifier import Verifier
from tests.portable import file_exists_command, yaml_quote


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class SimpleRunner:
    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        (workspace / f"out-{task.id.lower()}.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


def _init_two_stage_repo(
    root: Path,
    *,
    self_hosting_enabled: bool = False,
) -> tuple[HarnessConfig, Roadmap, StateStore, WorktreeManager, Verifier, FailureStore]:
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    write(root / ".gitignore", ".harness/\n")
    write(root / "AGENTS.md", "# test\n")
    self_host_block = ""
    if self_hosting_enabled:
        self_host_block = """
self_hosting:
  enabled: true
"""
    write(
        root / "harness.yaml",
        f"""
execution:
  max_parallel_agents: 1
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
{self_host_block}
""",
    )
    for task_id in ("A", "B"):
        output = f"out-{task_id.lower()}.txt"
        check = file_exists_command(output)
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
    write(
        root / "tasks/roadmap.yaml",
        """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
      B: {file: tasks/B.md, stage: 2, depends_on: [A]}
    stage_verification:
      "1": []
      "2": []
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


def test_self_hosting_defaults_disabled(tmp_path: Path):
    write(tmp_path / "harness.yaml", "execution: {}\n")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    assert config.self_hosting == SelfHostingConfig()
    assert config.self_hosting.enabled is False


def test_ensure_frozen_copy_installs_prunes_and_skips(tmp_path: Path):
    commit = "abcdef0123456789deadbeef"
    calls: list[list[str]] = []

    def fake_pip(command: list[str], cwd: Path) -> None:
        calls.append(command)
        target = Path(command[command.index("--target") + 1])
        target.mkdir(parents=True, exist_ok=True)
        (target / "harness").mkdir()
        assert cwd == tmp_path.resolve()

    # Seed older copies so prune keeps only the newest three.
    root = self_root(tmp_path)
    for name, age in (("old1", 40), ("old2", 30), ("old3", 20), ("old4", 10)):
        path = root / name
        path.mkdir(parents=True)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))

    first = ensure_frozen_copy(tmp_path, commit, pip_install=fake_pip)
    expected = frozen_copy_path(tmp_path, commit)
    assert first == expected
    assert expected.is_dir()
    assert len(calls) == 1
    assert calls[0][1:5] == ["-m", "pip", "install", "--no-deps"]
    assert calls[0][calls[0].index("--target") + 1] == str(expected)
    assert calls[0][-1] == str(tmp_path.resolve())
    remaining = sorted(p.name for p in root.iterdir() if p.is_dir())
    assert short_commit(commit) in remaining
    assert len(remaining) == 3

    second = ensure_frozen_copy(tmp_path, commit, pip_install=fake_pip)
    assert second == expected
    assert len(calls) == 1  # skipped reinstall


def test_ensure_frozen_copy_failure_returns_none_and_warns(tmp_path: Path, caplog):
    def boom(command: list[str], cwd: Path) -> None:
        raise RuntimeError("pip exploded")

    with caplog.at_level("WARNING"):
        assert ensure_frozen_copy(tmp_path, "abc1234567890", pip_install=boom) is None
    assert "continuing on current code" in caplog.text
    assert not frozen_copy_path(tmp_path, "abc1234567890").exists()


def test_prune_frozen_copies_keeps_newest_three(tmp_path: Path):
    root = tmp_path / "self"
    for index, age in enumerate((50, 40, 30, 20, 10), start=1):
        path = root / f"c{index}"
        path.mkdir(parents=True)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
    prune_frozen_copies(root, keep=3)
    assert sorted(p.name for p in root.iterdir()) == ["c3", "c4", "c5"]


def test_run_resume_sets_pythonpath_and_returns_child_code(tmp_path: Path):
    target = tmp_path / "frozen"
    target.mkdir()
    seen: dict[str, object] = {}

    def fake_child(command: list[str], env: dict[str, str]) -> int:
        seen["command"] = command
        seen["env"] = env
        return 42

    code = run_resume_with_frozen_copy(
        "s1",
        target,
        repo_root=tmp_path,
        child_runner=fake_child,
        environ={"PATH": "/usr/bin", "KEEP": "1"},
    )
    assert code == 42
    command = seen["command"]
    assert isinstance(command, list)
    assert command[1:4] == ["-P", "-m", "harness"]
    assert "resume" in command and "s1" in command
    assert "--root" in command and str(tmp_path) in command
    env = seen["env"]
    assert isinstance(env, dict)
    assert env["PYTHONPATH"] == str(target)
    assert env["KEEP"] == "1"


def test_scheduler_restart_after_stage_when_enabled(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = _init_two_stage_repo(
        tmp_path, self_hosting_enabled=True
    )
    scheduler = Scheduler(
        tmp_path, config, state, worktrees, SimpleRunner(), verifier, failures=failures, roadmap=roadmap
    )
    sprint = roadmap.sprint("s1")
    assert scheduler.run_sprint(sprint) is False
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("B").status == TaskStatus.PENDING
    assert failures.get("s1")["kind"] == "RESTART_ON_NEW_CODE"
    texts = [item["text"] for item in read_events(tmp_path, "s1", limit=50)]
    assert "restart on new code" in texts

    # Stage 2 is last: resume completes without another restart.
    assert scheduler.run_sprint(sprint) is True
    assert state.get("B").status == TaskStatus.DONE


def test_scheduler_disabled_runs_all_stages(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = _init_two_stage_repo(
        tmp_path, self_hosting_enabled=False
    )
    assert config.self_hosting.enabled is False
    scheduler = Scheduler(
        tmp_path, config, state, worktrees, SimpleRunner(), verifier, failures=failures, roadmap=roadmap
    )
    assert scheduler.run_sprint(roadmap.sprint("s1")) is True
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("B").status == TaskStatus.DONE
    assert failures.get("s1") is None


def test_cli_restarts_on_frozen_copy_after_stage(tmp_path: Path, monkeypatch):
    config, roadmap, state, worktrees, verifier, failures = _init_two_stage_repo(
        tmp_path, self_hosting_enabled=True
    )
    scheduler = Scheduler(
        tmp_path, config, state, worktrees, SimpleRunner(), verifier, failures=failures, roadmap=roadmap
    )
    pip_calls: list[str] = []
    child_calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_ensure(repo_root: Path, commit: str, **kwargs):
        target = frozen_copy_path(repo_root, commit)
        target.mkdir(parents=True, exist_ok=True)
        pip_calls.append(commit)
        return target

    def fake_resume(sprint_id: str, target: Path, **kwargs) -> int:
        child_calls.append(
            (
                resume_command(sprint_id, repo_root=kwargs.get("repo_root"), roadmap=kwargs.get("roadmap", "tasks/roadmap.yaml"), config=kwargs.get("config", "harness.yaml")),
                {"PYTHONPATH": str(target)},
            )
        )
        return 17

    keep_events: list[object] = []

    class FakeKeepAwake:
        def __init__(self, enabled: bool):
            keep_events.append(("init", enabled))

        def __enter__(self):
            keep_events.append("enter")
            return self

        def __exit__(self, exc_type, exc, tb):
            keep_events.append("exit")

    manifests = SimpleNamespace(
        start_or_resume=lambda **kwargs: tmp_path / ".harness" / "manifest.json",
        finish=lambda *a, **k: None,
        set_status=lambda *a, **k: None,
    )
    (tmp_path / ".harness").mkdir(exist_ok=True)
    (tmp_path / ".harness" / "manifest.json").write_text("{}", encoding="utf-8")

    runtime = SimpleNamespace(
        root=tmp_path,
        roadmap_path=tmp_path / "tasks/roadmap.yaml",
        config_path=tmp_path / "harness.yaml",
        config=config,
        roadmap=roadmap,
        state=state,
        worktrees=worktrees,
        failures=failures,
        scheduler=scheduler,
        manifests=manifests,
        validator=SimpleNamespace(validate=lambda sprint_id: SimpleNamespace(ok=True, render=lambda: "")),
        plan_changes=SimpleNamespace(ensure_baseline=lambda roadmap: None),
        quota=SimpleNamespace(),
        capacity=SimpleNamespace(),
    )

    monkeypatch.setattr(cli, "KeepAwake", FakeKeepAwake)
    monkeypatch.setattr(cli, "ensure_frozen_copy", fake_ensure)
    monkeypatch.setattr(cli, "run_resume_with_frozen_copy", fake_resume)
    monkeypatch.setattr(cli, "_prepare_sprint_base", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_run_pending_revalidations", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_check_plan_gate", lambda *a, **k: None)

    assert cli.cmd_run(runtime, "s1", dry_run=False) == 17
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("B").status == TaskStatus.PENDING
    assert len(pip_calls) == 1
    assert short_commit(pip_calls[0]) == short_commit(worktrees.head_commit())
    assert len(child_calls) == 1
    command, env = child_calls[0]
    assert command[1:4] == ["-P", "-m", "harness"]
    assert env["PYTHONPATH"] == str(frozen_copy_path(tmp_path, pip_calls[0]))
    assert keep_events == [("init", True), "enter", "exit"]


def test_cli_continues_on_current_code_when_build_fails(tmp_path: Path, monkeypatch):
    config, roadmap, state, worktrees, verifier, failures = _init_two_stage_repo(
        tmp_path, self_hosting_enabled=True
    )
    scheduler = Scheduler(
        tmp_path, config, state, worktrees, SimpleRunner(), verifier, failures=failures, roadmap=roadmap
    )

    def fake_ensure(repo_root: Path, commit: str, **kwargs):
        return None

    def boom_resume(*args, **kwargs):
        raise AssertionError("resume must not start when frozen copy failed")

    class FakeKeepAwake:
        def __init__(self, enabled: bool):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    manifests = SimpleNamespace(
        start_or_resume=lambda **kwargs: tmp_path / ".harness" / "manifest.json",
        finish=lambda *a, **k: None,
        set_status=lambda *a, **k: None,
    )
    (tmp_path / ".harness").mkdir(exist_ok=True)
    (tmp_path / ".harness" / "manifest.json").write_text("{}", encoding="utf-8")

    runtime = SimpleNamespace(
        root=tmp_path,
        roadmap_path=tmp_path / "tasks/roadmap.yaml",
        config_path=tmp_path / "harness.yaml",
        config=config,
        roadmap=roadmap,
        state=state,
        worktrees=worktrees,
        failures=failures,
        scheduler=scheduler,
        manifests=manifests,
        validator=SimpleNamespace(validate=lambda sprint_id: SimpleNamespace(ok=True, render=lambda: "")),
        plan_changes=SimpleNamespace(ensure_baseline=lambda roadmap: None),
        quota=SimpleNamespace(),
        capacity=SimpleNamespace(),
    )

    monkeypatch.setattr(cli, "KeepAwake", FakeKeepAwake)
    monkeypatch.setattr(cli, "ensure_frozen_copy", fake_ensure)
    monkeypatch.setattr(cli, "run_resume_with_frozen_copy", boom_resume)
    monkeypatch.setattr(cli, "_prepare_sprint_base", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_run_pending_revalidations", lambda *a, **k: True)
    monkeypatch.setattr(cli, "_check_plan_gate", lambda *a, **k: None)

    assert cli.cmd_run(runtime, "s1", dry_run=False) == 0
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("B").status == TaskStatus.DONE
    assert failures.get("s1") is None
