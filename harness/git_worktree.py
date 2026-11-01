from __future__ import annotations

import os
import re
import shutil
import subprocess
import threading
from pathlib import Path


def _run(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=cwd, text=True, encoding="utf-8", errors="replace", capture_output=True, check=check)


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip("-")


class GitError(RuntimeError):
    pass


class WorktreeManager:
    def __init__(self, repo_root: Path, runtime_root: Path):
        self.repo_root = repo_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.worktree_root = self.runtime_root / "worktrees"
        self.worktree_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def ensure_repo(self) -> None:
        result = _run(["git", "rev-parse", "--show-toplevel"], self.repo_root, check=False)
        if result.returncode != 0:
            raise GitError("Harness must run inside a Git repository")
        actual = Path(result.stdout.strip()).resolve()
        if actual != self.repo_root:
            raise GitError(f"Run the harness from the Git root: {actual}")

    def current_branch(self) -> str:
        result = _run(["git", "branch", "--show-current"], self.repo_root)
        branch = result.stdout.strip()
        if not branch:
            raise GitError("Detached HEAD is not supported as the integration base")
        return branch

    def head_commit(self) -> str:
        return _run(["git", "rev-parse", "HEAD"], self.repo_root).stdout.strip()

    def resolve_ref(self, ref: str) -> str:
        result = _run(["git", "rev-parse", "--verify", f"{ref}^{{commit}}"], self.repo_root, check=False)
        if result.returncode != 0:
            raise GitError(f"Configured base_ref does not resolve to a commit: {ref}")
        return result.stdout.strip()

    def is_clean(self) -> bool:
        result = _run(["git", "status", "--porcelain"], self.repo_root)
        # Runtime state is gitignored, so any visible line is meaningful.
        return not result.stdout.strip()

    def create(self, task_id: str, base_ref: str) -> tuple[str, Path]:
        with self._lock:
            branch = f"agent/{_slug(task_id)}"
            path = self.worktree_root / _slug(task_id)
            if path.exists():
                return branch, path

            # Remove stale branch if it exists but is not checked out.
            _run(["git", "branch", "-D", branch], self.repo_root, check=False)
            result = _run(
                ["git", "worktree", "add", "-b", branch, str(path), base_ref],
                self.repo_root,
                check=False,
            )
            if result.returncode != 0:
                raise GitError(result.stderr.strip() or result.stdout.strip())
            return branch, path

    def changed_files(self, worktree: Path, base_ref: str) -> list[str]:
        tracked = _run(["git", "diff", "--name-only", base_ref, "--"], worktree)
        untracked = _run(["git", "ls-files", "--others", "--exclude-standard"], worktree)
        files = {line.strip() for line in tracked.stdout.splitlines() if line.strip()}
        files.update(line.strip() for line in untracked.stdout.splitlines() if line.strip())
        return sorted(files)

    def commit_all(self, worktree: Path, task_id: str) -> str:
        _run(["git", "add", "-A"], worktree)
        status = _run(["git", "status", "--porcelain"], worktree)
        if not status.stdout.strip():
            # Valid for tasks that only verify existing state.
            return _run(["git", "rev-parse", "HEAD"], worktree).stdout.strip()

        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "Agent Harness")
        env.setdefault("GIT_AUTHOR_EMAIL", "harness@local")
        env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
        env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
        result = subprocess.run(
            ["git", "commit", "-m", f"harness: {task_id}"],
            cwd=worktree,
            text=True, encoding="utf-8", errors="replace",
            capture_output=True,
            env=env,
        )
        if result.returncode != 0:
            raise GitError(result.stderr.strip() or result.stdout.strip())
        return _run(["git", "rev-parse", "HEAD"], worktree).stdout.strip()

    def merge_branch(self, branch: str, task_id: str) -> None:
        result = _run(
            ["git", "merge", "--no-ff", branch, "-m", f"harness: integrate {task_id}"],
            self.repo_root,
            check=False,
        )
        if result.returncode != 0:
            _run(["git", "merge", "--abort"], self.repo_root, check=False)
            raise GitError(f"Merge failed for {task_id}: {result.stderr.strip() or result.stdout.strip()}")

    def rollback_integration(self, checkpoint: str) -> None:
        """Restore the integration checkout exactly to the pre-stage checkpoint."""
        with self._lock:
            _run(["git", "merge", "--abort"], self.repo_root, check=False)
            result = _run(["git", "reset", "--hard", checkpoint], self.repo_root, check=False)
            if result.returncode != 0:
                raise GitError(
                    f"Failed to roll back integration checkout to {checkpoint}: "
                    f"{result.stderr.strip() or result.stdout.strip()}"
                )
            # The integration checkout is required to be clean before a stage,
            # so any non-ignored untracked files now are stage-generated and may
            # be removed safely as part of the rollback.
            _run(["git", "clean", "-fd"], self.repo_root, check=False)
            actual = self.head_commit()
            if actual != checkpoint:
                raise GitError(f"Rollback verification failed: expected {checkpoint}, found {actual}")
            if not self.is_clean():
                raise GitError("Rollback verification failed: integration checkout is still dirty")


    def registered_worktrees(self) -> set[Path]:
        result = _run(["git", "worktree", "list", "--porcelain"], self.repo_root, check=False)
        paths: set[Path] = set()
        if result.returncode != 0:
            return paths
        for line in result.stdout.splitlines():
            if line.startswith("worktree "):
                paths.add(Path(line[len("worktree "):]).resolve())
        return paths

    def cleanup_stale_worktree_dirs(self) -> list[Path]:
        """Prune Git metadata and remove only unregistered harness-owned directories."""
        with self._lock:
            _run(["git", "worktree", "prune"], self.repo_root, check=False)
            registered = self.registered_worktrees()
            removed: list[Path] = []
            if not self.worktree_root.exists():
                return removed
            for child in self.worktree_root.iterdir():
                if not child.is_dir():
                    continue
                if child.resolve() in registered:
                    continue
                shutil.rmtree(child, ignore_errors=True)
                removed.append(child)
            return removed

    def delete_branch(self, branch: str) -> None:
        _run(["git", "branch", "-D", branch], self.repo_root, check=False)

    def remove(self, worktree: Path, branch: str, *, delete_branch: bool = True) -> None:
        _run(["git", "worktree", "remove", "--force", str(worktree)], self.repo_root, check=False)
        if worktree.exists():
            shutil.rmtree(worktree, ignore_errors=True)
        if delete_branch:
            _run(["git", "branch", "-D", branch], self.repo_root, check=False)
