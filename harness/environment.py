from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import HarnessConfig
from .logging_utils import write_log
from .models import TaskSpec
from .process_utils import prepare_external_argv, split_command


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "task"


@dataclass(slots=True)
class EnvironmentResult:
    ok: bool
    message: str


class EnvironmentManager:
    """Task/stage environment isolation and optional Docker Compose lifecycle.

    The generic harness remains Docker-optional. A task opts in with
    `environment: docker`; only then do Compose validation/up/down actions run.
    Every task and stage still receives a deterministic COMPOSE_PROJECT_NAME so
    commands that invoke Docker themselves do not collide with parallel work.
    """

    def __init__(self, root: Path, config: HarnessConfig, log_dir: Path):
        self.root = root.resolve()
        self.config = config
        self.log_dir = log_dir

    def _project_name(self, label: str) -> str:
        prefix = _slug(self.config.environment.project_name_prefix)
        return f"{prefix}-{_slug(label)}"[:63].rstrip("-")

    def task_env(self, task: TaskSpec, workspace: Path) -> dict[str, str]:
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in self.config.environment.env.items()})
        env.update(
            {
                "HARNESS_TASK_ID": task.id,
                "HARNESS_SPRINT_ID": task.sprint,
                "HARNESS_STAGE": str(task.stage),
                "HARNESS_WORKTREE": str(workspace),
                "COMPOSE_PROJECT_NAME": self._project_name(task.id),
            }
        )
        return env

    def stage_env(self, sprint_id: str, stage: int, workspace: Path) -> dict[str, str]:
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in self.config.environment.env.items()})
        env.update(
            {
                "HARNESS_SPRINT_ID": sprint_id,
                "HARNESS_STAGE": str(stage),
                "HARNESS_WORKTREE": str(workspace),
                "COMPOSE_PROJECT_NAME": self._project_name(f"{sprint_id}-stage-{stage}"),
            }
        )
        return env

    def _compose_base(self) -> list[str]:
        command = split_command(self.config.environment.compose_command)
        for compose_file in self.config.environment.compose_files:
            command += ["-f", compose_file]
        return command

    def _run_compose(
        self,
        args: list[str],
        *,
        workspace: Path,
        env: dict[str, str],
        log_name: str,
        timeout_seconds: int,
    ) -> EnvironmentResult:
        command = [*self._compose_base(), *args]
        try:
            proc = subprocess.run(
                prepare_external_argv(command),
                cwd=workspace,
                text=True, encoding="utf-8", errors="replace",
                capture_output=True,
                env=env,
                timeout=max(1, timeout_seconds),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            rendered = f"$ {' '.join(command)}\nERROR: {exc}\n"
            write_log(self.log_dir, log_name, rendered)
            return EnvironmentResult(False, str(exc))
        rendered = f"$ {' '.join(command)}\nexit={proc.returncode}\n{proc.stdout}{proc.stderr}".rstrip() + "\n"
        write_log(self.log_dir, log_name, rendered)
        if proc.returncode != 0:
            return EnvironmentResult(False, proc.stderr.strip() or proc.stdout.strip() or "docker compose failed")
        return EnvironmentResult(True, "ok")

    def prepare_task(self, task: TaskSpec, workspace: Path) -> EnvironmentResult:
        if task.environment != "docker":
            return EnvironmentResult(True, "no managed environment requested")
        if not self.config.environment.docker_enabled:
            return EnvironmentResult(False, "Task requests environment=docker but environment.docker_enabled=false")

        env = self.task_env(task, workspace)
        timeout = self.config.environment.command_timeout_minutes * 60
        if self.config.environment.validate_compose:
            result = self._run_compose(
                ["config", "--quiet"],
                workspace=workspace,
                env=env,
                log_name=f"{task.id}-environment-config.log",
                timeout_seconds=timeout,
            )
            if not result.ok:
                return EnvironmentResult(False, f"Docker Compose config validation failed: {result.message}")

        if self.config.environment.up_before_agent:
            args = ["up", "-d", "--remove-orphans"]
            args += list(self.config.environment.services)
            result = self._run_compose(
                args,
                workspace=workspace,
                env=env,
                log_name=f"{task.id}-environment-up.log",
                timeout_seconds=timeout,
            )
            if not result.ok:
                return EnvironmentResult(False, f"Docker environment startup failed: {result.message}")
        return EnvironmentResult(True, "docker environment ready")

    def teardown_task(self, task: TaskSpec, workspace: Path, *, blocked: bool = False) -> EnvironmentResult:
        if task.environment != "docker" or not self.config.environment.docker_enabled:
            return EnvironmentResult(True, "no managed environment")
        if blocked and not self.config.environment.teardown_on_blocked:
            return EnvironmentResult(True, "retained blocked task environment")
        if not blocked and not self.config.environment.teardown_on_success:
            return EnvironmentResult(True, "retained successful task environment")
        env = self.task_env(task, workspace)
        args = ["down", "--remove-orphans"]
        if self.config.environment.remove_volumes:
            args.append("--volumes")
        return self._run_compose(
            args,
            workspace=workspace,
            env=env,
            log_name=f"{task.id}-environment-down.log",
            timeout_seconds=self.config.environment.command_timeout_minutes * 60,
        )
