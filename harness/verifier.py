from __future__ import annotations

import fnmatch
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import HarnessConfig
from .git_worktree import WorktreeManager
from .logging_utils import write_log
from .models import TaskSpec


@dataclass(slots=True)
class VerificationResult:
    ok: bool
    summary: str
    failures: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)


def _matches(path: str, pattern: str) -> bool:
    normalized = pattern.rstrip("/")
    if normalized.endswith("/**"):
        prefix = normalized[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatch(path, normalized)


class Verifier:
    def __init__(self, config: HarnessConfig, log_dir: Path, worktrees: WorktreeManager):
        self.config = config
        self.log_dir = log_dir
        self.worktrees = worktrees


    def _verification_env(self, env: dict[str, str] | None) -> dict[str, str]:
        # When the scheduler passes an explicit task/stage environment it is
        # already complete (EnvironmentManager starts from os.environ). Treat
        # that mapping as authoritative instead of consulting ambient process
        # state a second time; this keeps verification deterministic and makes
        # tests/embedded callers able to supply an isolated environment.
        merged = (
            {str(k): str(v) for k, v in env.items()}
            if env is not None
            else os.environ.copy()
        )
        # Verification fallbacks fill only missing values. A real task/runtime
        # environment always wins, so static validation helpers cannot replace
        # actual Docker/database credentials.
        for key, value in self.config.verification.env.items():
            merged.setdefault(str(key), str(value))
        return merged

    def requires_review(self, changed_files: list[str]) -> bool:
        return any(
            _matches(path, pattern)
            for path in changed_files
            for pattern in self.config.paths.review_required
        )

    def verify_task(
        self, task: TaskSpec, workspace: Path, base_ref: str, *, env: dict[str, str] | None = None
    ) -> VerificationResult:
        failures: list[str] = []
        changed = self.worktrees.changed_files(workspace, base_ref)

        if task.allowed_paths:
            out_of_scope = [
                path for path in changed
                if not any(_matches(path, pattern) for pattern in task.allowed_paths)
            ]
            if out_of_scope:
                failures.append("Changed files outside allowed_paths: " + ", ".join(out_of_scope))

        if not task.allow_protected:
            protected = [
                path for path in changed
                if any(_matches(path, pattern) for pattern in self.config.paths.protected)
            ]
            if protected:
                failures.append("Protected files changed: " + ", ".join(protected))

        command_outputs: list[str] = []
        commands = [*self.config.verification.global_task_commands, *task.verification]
        command_env = self._verification_env(env)
        for command in commands:
            proc = subprocess.run(command, cwd=workspace, text=True, encoding="utf-8", errors="replace", shell=True, capture_output=True, env=command_env)
            rendered = f"$ {command}\nexit={proc.returncode}\n{proc.stdout}{proc.stderr}".rstrip()
            command_outputs.append(rendered)
            if proc.returncode != 0:
                failures.append(f"Verification command failed: {command}")

        summary = "\n\n".join([
            "Changed files:\n" + ("\n".join(changed) if changed else "(none)"),
            *command_outputs,
            "Failures:\n" + ("\n".join(failures) if failures else "(none)"),
        ])
        write_log(self.log_dir, f"{task.id}-verification.log", summary)
        return VerificationResult(not failures, summary, failures, changed)


    def verify_integrated_task(
        self, task: TaskSpec, workspace: Path, *, env: dict[str, str] | None = None
    ) -> VerificationResult:
        """Re-run a DONE task's behavioral verification on the integrated checkout.

        This deliberately skips diff/scope checks because the task is already merged;
        the purpose is regression validation after an approved plan revision.
        """
        failures: list[str] = []
        outputs: list[str] = []
        command_env = self._verification_env(env)
        for command in [*self.config.verification.global_task_commands, *task.verification]:
            proc = subprocess.run(command, cwd=workspace, text=True, encoding="utf-8", errors="replace", shell=True, capture_output=True, env=command_env)
            outputs.append(f"$ {command}\nexit={proc.returncode}\n{proc.stdout}{proc.stderr}".rstrip())
            if proc.returncode != 0:
                failures.append(f"Revalidation command failed: {command}")
        summary = "\n\n".join([*outputs, "Failures:\n" + ("\n".join(failures) if failures else "(none)")])
        write_log(self.log_dir, f"{task.id}-revalidation.log", summary)
        return VerificationResult(not failures, summary, failures)

    def verify_stage(
        self, workspace: Path, commands: list[str], stage_name: str, *, env: dict[str, str] | None = None
    ) -> VerificationResult:
        failures: list[str] = []
        outputs: list[str] = []
        command_env = self._verification_env(env)
        for command in [*self.config.verification.stage_commands, *commands]:
            proc = subprocess.run(command, cwd=workspace, text=True, encoding="utf-8", errors="replace", shell=True, capture_output=True, env=command_env)
            outputs.append(f"$ {command}\nexit={proc.returncode}\n{proc.stdout}{proc.stderr}".rstrip())
            if proc.returncode != 0:
                failures.append(f"Stage command failed: {command}")
        summary = "\n\n".join([*outputs, "Failures:\n" + ("\n".join(failures) if failures else "(none)")])
        write_log(self.log_dir, f"{stage_name}-verification.log", summary)
        return VerificationResult(not failures, summary, failures)
