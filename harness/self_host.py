from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

LOGGER = logging.getLogger(__name__)

SHORT_COMMIT_LEN = 12
KEEP_FROZEN_COPIES = 3

PipInstall = Callable[[list[str], Path], None]
ChildRunner = Callable[[list[str], Mapping[str, str]], int]


def short_commit(commit: str) -> str:
    return commit.strip()[:SHORT_COMMIT_LEN]


def self_root(repo_root: Path) -> Path:
    return repo_root / ".harness" / "self"


def frozen_copy_path(repo_root: Path, commit: str) -> Path:
    return self_root(repo_root) / short_commit(commit)


def _default_pip_install(command: list[str], cwd: Path) -> None:
    completed = subprocess.run(
        command,
        cwd=cwd,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            f"pip install failed (exit {completed.returncode})"
            + (f": {detail}" if detail else "")
        )


def prune_frozen_copies(root: Path, *, keep: int = KEEP_FROZEN_COPIES) -> None:
    if keep < 1 or not root.is_dir():
        return
    dirs = [path for path in root.iterdir() if path.is_dir()]
    dirs.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    for stale in dirs[keep:]:
        shutil.rmtree(stale, ignore_errors=True)


def ensure_frozen_copy(
    repo_root: Path,
    commit: str,
    *,
    python_executable: str | None = None,
    pip_install: PipInstall | None = None,
) -> Path | None:
    """Install a frozen harness copy for ``commit`` under ``.harness/self/``.

    Returns the target directory on success (including when it already existed).
    On failure logs a warning and returns ``None`` so the sprint can continue
    on the currently running code.
    """
    repo_root = repo_root.resolve()
    target = frozen_copy_path(repo_root, commit)
    install = pip_install or _default_pip_install
    python = python_executable or sys.executable

    try:
        if target.is_dir():
            prune_frozen_copies(self_root(repo_root))
            return target

        target.parent.mkdir(parents=True, exist_ok=True)
        command = [
            python,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(target),
            str(repo_root),
        ]
        install(command, repo_root)
        if not target.is_dir():
            raise RuntimeError(f"pip install did not create target {target}")
        prune_frozen_copies(self_root(repo_root))
        return target
    except Exception as exc:
        LOGGER.warning(
            "Self-host frozen copy failed for %s; continuing on current code: %s",
            short_commit(commit),
            exc,
        )
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        return None


def resume_command(
    sprint_id: str,
    *,
    python_executable: str | None = None,
    repo_root: Path | None = None,
    roadmap: str = "tasks/roadmap.yaml",
    config: str = "harness.yaml",
) -> list[str]:
    command = [python_executable or sys.executable, "-P", "-m", "harness"]
    if repo_root is not None:
        command.extend(["--root", str(repo_root)])
    if roadmap != "tasks/roadmap.yaml":
        command.extend(["--roadmap", roadmap])
    if config != "harness.yaml":
        command.extend(["--config", config])
    command.extend(["resume", sprint_id])
    return command


def _default_child_runner(command: list[str], env: Mapping[str, str]) -> int:
    completed = subprocess.run(command, check=False, env=dict(env))
    return int(completed.returncode)


def run_resume_with_frozen_copy(
    sprint_id: str,
    target: Path,
    *,
    repo_root: Path | None = None,
    roadmap: str = "tasks/roadmap.yaml",
    config: str = "harness.yaml",
    python_executable: str | None = None,
    environ: Mapping[str, str] | None = None,
    child_runner: ChildRunner | None = None,
) -> int:
    """Start ``python -P -m harness resume`` with PYTHONPATH set to ``target``."""
    env = dict(environ if environ is not None else os.environ)
    env["PYTHONPATH"] = str(target)
    command = resume_command(
        sprint_id,
        python_executable=python_executable,
        repo_root=repo_root,
        roadmap=roadmap,
        config=config,
    )
    runner = child_runner or _default_child_runner
    return runner(command, env)
