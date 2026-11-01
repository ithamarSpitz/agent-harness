from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from harness.cli import _prepare_sprint_base
from harness.config import HarnessConfig
from harness.git_worktree import WorktreeManager
from harness.state import StateStore


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def init_repo(tmp_path: Path) -> None:
    run(["git", "init", "-b", "main"], tmp_path)
    run(["git", "config", "user.name", "Test"], tmp_path)
    run(["git", "config", "user.email", "test@example.com"], tmp_path)
    write(tmp_path / ".gitignore", ".harness/\n")
    write(tmp_path / "a.txt", "one\n")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "one"], tmp_path)


def config(tmp_path: Path, base_ref: str | None) -> HarnessConfig:
    rendered = "null" if base_ref is None else base_ref
    write(tmp_path / "harness.yaml", f"""
execution:
  base_ref: {rendered}
worktree: {{}}
cursor: {{models: {{}}}}
verification: {{}}
paths: {{}}
""")
    return HarnessConfig.load(tmp_path / "harness.yaml")


def test_base_ref_accepts_matching_start_and_allows_resume_after_head_advances(tmp_path: Path):
    init_repo(tmp_path)
    cfg = config(tmp_path, "main")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")

    _prepare_sprint_base(cfg, state, worktrees, "s1")
    initial = state.get_meta("s1.initial_base_commit")
    assert initial == run(["git", "rev-parse", "HEAD"], tmp_path)

    write(tmp_path / "b.txt", "two\n")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "two"], tmp_path)

    # A resume is allowed to be ahead of the initial base after completed stages.
    _prepare_sprint_base(cfg, state, worktrees, "s1")


def test_base_ref_rejects_wrong_initial_commit(tmp_path: Path):
    init_repo(tmp_path)
    initial = run(["git", "rev-parse", "HEAD"], tmp_path)
    run(["git", "tag", "expected", initial], tmp_path)
    write(tmp_path / "b.txt", "two\n")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "two"], tmp_path)

    cfg = config(tmp_path, "expected")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    with pytest.raises(RuntimeError, match="base_ref"):
        _prepare_sprint_base(cfg, state, worktrees, "s1")


def test_base_ref_cannot_change_mid_sprint(tmp_path: Path):
    init_repo(tmp_path)
    cfg = config(tmp_path, "main")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    _prepare_sprint_base(cfg, state, worktrees, "s1")

    cfg.execution.base_ref = None
    with pytest.raises(RuntimeError, match="changed"):
        _prepare_sprint_base(cfg, state, worktrees, "s1")
