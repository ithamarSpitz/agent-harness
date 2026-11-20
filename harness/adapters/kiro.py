from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Callable

from ..agent_runner import AgentResult
from ..config import HarnessConfig
from ..kiro_runner import KiroAgentRunner
from ..models import TaskSpec
from .base import BaseAdapter, CallContext, FindResult, Levels, command_version


class KiroAdapter(BaseAdapter):
    name = "kiro"

    def __init__(self, runner: KiroAgentRunner | None, config: HarnessConfig) -> None:
        self.runner = runner
        self.config = config
        self.command = config.kiro.command

    def enabled(self) -> bool:
        return bool(self.config.kiro.enabled and self.runner is not None)

    def _signed_in(self) -> tuple[bool | None, str]:
        # whoami output contains the account e-mail: never keep or report it.
        try:
            completed = subprocess.run(
                [self.command, "whoami"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            return None, type(error).__name__
        if completed.returncode != 0:
            return False, "not signed in"
        if "Logged in" in (completed.stdout or ""):
            return True, "signed in"
        return None, ""

    def find(self, *, refresh: bool = False) -> FindResult:
        found = command_version(self.command)
        if not found.installed:
            return FindResult(False, None, None, found.detail)
        signed_in, detail = self._signed_in()
        return FindResult(True, found.version, signed_in, detail)

    def implementation_budget(self) -> int:
        return len(self.config.kiro.implementation_sequence)

    def levels(self) -> Levels:
        cfg = self.config.kiro
        sequence = cfg.implementation_sequence
        return Levels(
            worker=sequence[0] if sequence else None,
            hard_worker=sequence[1] if len(sequence) > 1 else (sequence[0] if sequence else None),
            escalation=cfg.escalation_model,
        )

    def _runner(self) -> KiroAgentRunner:
        if self.runner is None:
            raise RuntimeError("kiro runner is not available")
        return self.runner

    def implement(self, ctx: CallContext) -> AgentResult:
        return self._runner().implement(
            ctx.task,
            ctx.workspace,
            ctx.timeout_minutes,
            ctx.provider_attempt + 1,
            ctx.previous_failure,
            env=ctx.env,
            model_class_override=ctx.model_class,
            code_map_offered=ctx.code_map_offered,
            on_process_start=ctx.on_process_start,
        )

    def review(
        self,
        task: TaskSpec,
        workspace: Path,
        base_ref: str,
        verification_summary: str,
        timeout_minutes: int,
        *,
        env: dict[str, str] | None,
        code_map_offered: bool,
        on_process_start: Callable[[int], None] | None,
    ) -> AgentResult:
        return self._runner().review(
            task,
            workspace,
            base_ref,
            verification_summary,
            timeout_minutes,
            env=env,
            code_map_offered=code_map_offered,
            on_process_start=on_process_start,
        )

    def plan_change(
        self,
        task: TaskSpec,
        workspace: Path,
        failure_summary: str,
        timeout_minutes: int,
        *,
        model_class: str,
        env: dict[str, str] | None,
    ) -> AgentResult:
        return self._runner().plan_change(
            task,
            workspace,
            failure_summary,
            timeout_minutes,
            model_class=model_class,
            env=env,
        )

    def brief_check(
        self,
        task: TaskSpec,
        workspace: Path,
        brief_text: str,
        timeout_minutes: int,
        *,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        return self._runner().brief_check(
            task, workspace, brief_text, timeout_minutes, env=env
        )
