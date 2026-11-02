from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import harness.git_worktree
from harness.git_worktree import GitError, WorktreeManager


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def init_repo(root: Path) -> str:
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    (root / ".gitignore").write_text(".harness/\n", encoding="utf-8")
    (root / "tracked.txt").write_text("base\n", encoding="utf-8")
    run(["git", "add", "."], root)
    run(["git", "commit", "-m", "base"], root)
    return run(["git", "rev-parse", "HEAD"], root)


def worktree_records(root: Path) -> dict[Path, str]:
    output = run(["git", "worktree", "list", "--porcelain"], root)
    records: dict[Path, str] = {}
    path: Path | None = None
    for line in [*output.splitlines(), ""]:
        if not line:
            path = None
        elif line.startswith("worktree "):
            path = Path(line.removeprefix("worktree ")).resolve()
        elif path is not None and line.startswith("branch refs/heads/"):
            records[path] = line.removeprefix("branch refs/heads/")
    return records


def test_plain_folder_is_replaced_with_registered_worktree(tmp_path: Path) -> None:
    base = init_repo(tmp_path)
    manager = WorktreeManager(tmp_path, tmp_path / ".harness")
    stale = manager.worktree_root / "A"
    (stale / "graphify-out").mkdir(parents=True)
    (stale / "graphify-out" / "old.txt").write_text("stale\n", encoding="utf-8")

    branch, worktree = manager.create("A", base)

    assert branch == "agent/A"
    assert worktree == stale
    assert not (worktree / "graphify-out" / "old.txt").exists()
    assert worktree_records(tmp_path)[worktree.resolve()] == branch


def test_registered_worktree_on_expected_branch_is_reused(tmp_path: Path) -> None:
    base = init_repo(tmp_path)
    manager = WorktreeManager(tmp_path, tmp_path / ".harness")
    branch, worktree = manager.create("A", base)
    marker = worktree / "resume.txt"
    marker.write_text("keep\n", encoding="utf-8")

    assert manager.create("A", base) == (branch, worktree)
    assert marker.read_text(encoding="utf-8") == "keep\n"


def test_registered_worktree_on_other_branch_is_rejected(tmp_path: Path) -> None:
    base = init_repo(tmp_path)
    manager = WorktreeManager(tmp_path, tmp_path / ".harness")
    worktree = manager.worktree_root / "A"
    run(["git", "worktree", "add", "-b", "other", str(worktree), base], tmp_path)

    with pytest.raises(GitError, match=r"registered on branch other, expected agent/A"):
        manager.create("A", base)


def test_unregistered_folder_that_cannot_be_removed_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = init_repo(tmp_path)
    manager = WorktreeManager(tmp_path, tmp_path / ".harness")
    stale = manager.worktree_root / "A"
    stale.mkdir()
    monkeypatch.setattr(harness.git_worktree.shutil, "rmtree", lambda path: None)

    with pytest.raises(GitError, match=rf"{re.escape(str(stale))}.*close programs using it"):
        manager.create("A", base)
