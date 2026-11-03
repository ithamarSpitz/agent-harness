from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import harness.git_worktree
from harness.cli import build_runtime, cmd_reset_task
from harness.git_worktree import GitError, WorktreeManager, _rmtree_with_retries
from harness.models import TaskStatus
from tests.portable import make_python_script, yaml_quote


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def init_repo(root: Path) -> str:
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    (root / ".gitignore").write_text(".harness/\n", encoding="utf-8")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "."], root)
    run(["git", "commit", "-m", "base"], root)
    return run(["git", "rev-parse", "HEAD"], root)


def init_project(tmp_path: Path) -> None:
    run(["git", "init", "-b", "main"], tmp_path)
    run(["git", "config", "user.name", "Test"], tmp_path)
    run(["git", "config", "user.email", "test@example.com"], tmp_path)
    fake = make_python_script(
        tmp_path / "fake-agent.py",
        """import sys
if 'models' in sys.argv:
    print('cheap-model\\nreview-model')
elif '--version' in sys.argv:
    print('fake 1.0')
""",
    )
    write(tmp_path / ".gitignore", ".harness/\nfake-agent.py\n")
    write(tmp_path / "AGENTS.md", "# agents\n")
    write(
        tmp_path / "harness.yaml",
        f"""
execution: {{}}
worktree: {{}}
cursor:
  command: {yaml_quote(fake)}
  models: {{worker: cheap-model, reviewer: review-model}}
verification: {{}}
paths: {{}}
context: {{}}
""",
    )
    write(
        tmp_path / "tasks/A.md",
        """---
id: A
model_class: worker
allowed_paths: [out.txt]
verification: [echo ok]
---
# A
""",
    )
    write(
        tmp_path / "tasks/roadmap.yaml",
        """
project: x
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
""",
    )
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)


def _skip_git_worktree_remove(monkeypatch: pytest.MonkeyPatch) -> None:
    real_run = harness.git_worktree._run

    def fake_run(args: list[str], cwd: Path, check: bool = True):
        if len(args) >= 3 and args[0] == "git" and args[1] == "worktree" and args[2] == "remove":
            return subprocess.CompletedProcess(args, 0, "", "")
        return real_run(args, cwd, check=check)

    monkeypatch.setattr(harness.git_worktree, "_run", fake_run)


def test_rmtree_retries_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "held"
    target.mkdir()
    (target / "file.txt").write_text("x\n", encoding="utf-8")
    real_rmtree = harness.git_worktree.shutil.rmtree
    calls = {"n": 0}

    def flaky(path: Path | str, *args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            return
        real_rmtree(path)

    monkeypatch.setattr(harness.git_worktree.shutil, "rmtree", flaky)
    monkeypatch.setattr(harness.git_worktree.time, "sleep", lambda _seconds: None)

    _rmtree_with_retries(target)

    assert calls["n"] == 3
    assert not target.exists()


def test_remove_retries_then_succeeds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = init_repo(tmp_path)
    manager = WorktreeManager(tmp_path, tmp_path / ".harness")
    branch, worktree = manager.create("A", base)
    _skip_git_worktree_remove(monkeypatch)

    real_rmtree = harness.git_worktree.shutil.rmtree
    calls = {"n": 0}

    def flaky(path: Path | str, *args: object, **kwargs: object) -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            return
        real_rmtree(path)

    monkeypatch.setattr(harness.git_worktree.shutil, "rmtree", flaky)
    monkeypatch.setattr(harness.git_worktree.time, "sleep", lambda _seconds: None)

    manager.remove(worktree, branch, delete_branch=True)

    assert calls["n"] == 3
    assert not worktree.exists()


def test_reset_task_reports_path_when_remove_always_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    init_project(tmp_path)
    runtime = build_runtime(tmp_path, tmp_path / "tasks/roadmap.yaml", tmp_path / "harness.yaml")
    branch, worktree = runtime.worktrees.create("A", runtime.worktrees.head_commit())
    rt = runtime.state.get("A")
    rt.status = TaskStatus.BLOCKED
    rt.branch = branch
    rt.worktree = str(worktree)
    runtime.state.save()

    _skip_git_worktree_remove(monkeypatch)
    monkeypatch.setattr(harness.git_worktree.shutil, "rmtree", lambda path, *a, **k: None)
    monkeypatch.setattr(harness.git_worktree.time, "sleep", lambda _seconds: None)

    code = cmd_reset_task(runtime, "A")
    err = capsys.readouterr().err

    assert code != 0
    assert f"could not remove {worktree}" in err
    assert "close programs using it and run reset-task again" in err
    assert worktree.exists()
    assert runtime.state.get("A").status == TaskStatus.BLOCKED


def test_cleanup_stale_warns_and_continues_when_delete_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    init_repo(tmp_path)
    manager = WorktreeManager(tmp_path, tmp_path / ".harness")
    stale = manager.worktree_root / "stale-left"
    stale.mkdir(parents=True)
    (stale / "x.txt").write_text("x\n", encoding="utf-8")

    monkeypatch.setattr(harness.git_worktree.shutil, "rmtree", lambda path, *a, **k: None)
    monkeypatch.setattr(harness.git_worktree.time, "sleep", lambda _seconds: None)

    with caplog.at_level("WARNING", logger="harness.git_worktree"):
        removed = manager.cleanup_stale_worktree_dirs()

    assert removed == []
    assert stale.exists()
    assert any(f"could not remove {stale}" in record.getMessage() for record in caplog.records)


def test_rmtree_always_fails_raises_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "stuck"
    target.mkdir()
    monkeypatch.setattr(harness.git_worktree.shutil, "rmtree", lambda path, *a, **k: None)
    monkeypatch.setattr(harness.git_worktree.time, "sleep", lambda _seconds: None)

    with pytest.raises(GitError, match=rf"could not remove {re.escape(str(target))}"):
        _rmtree_with_retries(target)
