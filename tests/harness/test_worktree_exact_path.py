from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from harness.git_worktree import WorktreeManager, _exact_path_key, _is_link_path


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


def try_create_dir_link(link: Path, target: Path) -> None:
    """Create a directory symlink (or Windows junction). Skip when the OS refuses."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        # Prefer a junction: usually allowed without SeCreateSymbolicLinkPrivilege.
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            text=True,
            capture_output=True,
            check=False,
        )
        if completed.returncode == 0 and os.path.lexists(link):
            return
        try:
            os.symlink(str(target), str(link), target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"OS does not allow creating directory links: {exc}")
        return
    try:
        os.symlink(str(target), str(link), target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"OS does not allow creating directory links: {exc}")


def _replace_worktree_dir_with_link_to(worktree: Path, target: Path) -> None:
    """Leave Git metadata listing ``worktree``, but make that path a link to ``target``."""
    # Do not call git worktree remove: the listed path must stay registered.
    shutil.rmtree(worktree)
    assert not os.path.lexists(worktree)
    try_create_dir_link(worktree, target)
    assert _is_link_path(worktree)


def test_link_at_registered_task_path_is_replaced_without_touching_target(tmp_path: Path) -> None:
    """AC1: link at the listed task path must not be reused (Background bug)."""
    base = init_repo(tmp_path)
    manager = WorktreeManager(tmp_path, tmp_path / ".harness")
    other_branch, other = manager.create("other", base)
    marker = other / "keep-me.txt"
    marker.write_text("untouched\n", encoding="utf-8")

    task_branch, task = manager.create("task", base)
    assert task_branch == "agent/task"
    # Dangerous case: git still lists ``task``, but the path is now a link to ``other``.
    _replace_worktree_dir_with_link_to(task, other)
    assert manager._registered_branch(task) is None

    branch, worktree = manager.create("task", base)

    assert branch == "agent/task"
    assert worktree == task
    assert not _is_link_path(worktree)
    assert worktree.is_dir()
    assert not (worktree / "keep-me.txt").exists()
    assert marker.read_text(encoding="utf-8") == "untouched\n"
    assert other_branch == "agent/other"
    assert (other / "keep-me.txt").exists()
    assert manager.create("other", base) == (other_branch, other)


def test_cleanup_does_not_keep_link_pointing_at_registered_worktree(tmp_path: Path) -> None:
    """Decision 3: cleanup membership must not follow links."""
    base = init_repo(tmp_path)
    manager = WorktreeManager(tmp_path, tmp_path / ".harness")
    _, other = manager.create("other", base)
    marker = other / "keep-me.txt"
    marker.write_text("untouched\n", encoding="utf-8")

    link = manager.worktree_root / "stale-link"
    try_create_dir_link(link, other)
    assert _is_link_path(link)

    removed = manager.cleanup_stale_worktree_dirs()

    assert link in removed
    assert not os.path.lexists(link)
    assert marker.read_text(encoding="utf-8") == "untouched\n"
    assert manager.create("other", base) == ("agent/other", other)


def test_exact_registered_path_is_reused(tmp_path: Path) -> None:
    base = init_repo(tmp_path)
    manager = WorktreeManager(tmp_path, tmp_path / ".harness")
    branch, worktree = manager.create("A", base)
    marker = worktree / "resume.txt"
    marker.write_text("keep\n", encoding="utf-8")

    assert manager.create("A", base) == (branch, worktree)
    assert marker.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.skipif(os.name != "nt", reason="Drive-letter case only varies on Windows")
def test_exact_path_reuse_ignores_drive_letter_case(tmp_path: Path) -> None:
    base = init_repo(tmp_path)
    manager = WorktreeManager(tmp_path, tmp_path / ".harness")
    branch, worktree = manager.create("A", base)
    marker = worktree / "resume.txt"
    marker.write_text("keep\n", encoding="utf-8")

    flipped = str(worktree)
    if flipped[0].isupper():
        flipped = flipped[0].lower() + flipped[1:]
    else:
        flipped = flipped[0].upper() + flipped[1:]
    assert flipped != str(worktree)
    assert _exact_path_key(flipped) == _exact_path_key(worktree)
    assert manager._registered_branch(Path(flipped)) == branch
    assert manager.create("A", base) == (branch, worktree)
    assert marker.read_text(encoding="utf-8") == "keep\n"
