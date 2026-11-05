from __future__ import annotations

import subprocess
from pathlib import Path

from harness.config import HarnessConfig
from harness.doctor import Doctor, DoctorCheck
from harness.git_worktree import WorktreeManager


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def doctor_check(root: Path) -> DoctorCheck:
    config_path = root / "harness.yaml"
    config_path.write_text("{}\n", encoding="utf-8")
    config = HarnessConfig.load(config_path)
    worktrees = WorktreeManager(root, root / ".harness")
    checks = Doctor(root, config, worktrees).run()
    return {check.name: check for check in checks}["repo/harness-ignored"]


def init_repo(root: Path, gitignore: str) -> None:
    run(["git", "init", "-b", "main"], root)
    (root / ".gitignore").write_text(gitignore, encoding="utf-8")


def test_harness_directory_ignored_is_ok(tmp_path: Path):
    init_repo(tmp_path, ".harness/\n")

    check = doctor_check(tmp_path)

    assert check.ok
    assert not check.skipped
    assert Doctor.render([check]).startswith("OK")


def test_only_known_harness_file_ignored_warns_with_fix(tmp_path: Path):
    init_repo(tmp_path, ".harness/state.json\n")

    check = doctor_check(tmp_path)

    assert not check.ok
    assert not check.required
    assert "add `.harness/` to .gitignore (or .git/info/exclude)" in check.detail
    assert Doctor.render([check]).startswith("WARN")


def test_non_git_directory_is_skipped(tmp_path: Path):
    check = doctor_check(tmp_path)

    assert check.ok
    assert check.skipped
    assert Doctor.render([check]).startswith("SKIP")
