from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..agent_runner import AgentResult
from ..config import HarnessConfig
from ..models import TaskSpec
from .base import BaseAdapter, CallContext, FindResult, Levels, command_version


class CodexAdapter(BaseAdapter):
    name = "codex"

    def __init__(self, runner: Any, config: HarnessConfig) -> None:
        self.runner = runner
        self.config = config
        self.command = config.codex.command

    def enabled(self) -> bool:
        return self.config.codex.enabled

    def find(self, *, refresh: bool = False) -> FindResult:
        available, detail = self.runner.availability(refresh=refresh)
        if available:
            return FindResult(True, None, True, detail)
        found = command_version(self.config.codex.command)
        return FindResult(found.installed, found.version, False, detail)

    def implementation_budget(self) -> int:
        return len(self.config.codex.implementation_sequence)

    def levels(self) -> Levels:
        sequence = self.config.codex.implementation_sequence
        if not sequence:
            return Levels(None, None, None)
        count = len(sequence)
        return Levels(
            worker=self.config.codex.implementation_profile(0)[0],
            hard_worker=self.config.codex.implementation_profile(min(2, count - 1))[0],
            escalation=self.config.codex.implementation_profile(count - 1)[0],
        )

    @staticmethod
    def _unavailable() -> AgentResult:
        return AgentResult(
            False, "", "Codex CLI/auth unavailable",
            provider="codex", auth_failure=True,
            fallback_reason="auth_or_cli_unavailable",
        )

    def implement(self, ctx: CallContext) -> AgentResult:
        available, _ = self.runner.availability()
        if not available:
            return self._unavailable()
        model, effort = self.config.codex.implementation_profile(ctx.provider_attempt)
        return self.runner.implement(
            ctx.task, ctx.workspace, ctx.timeout_minutes, ctx.attempt, ctx.previous_failure,
            model=model, reasoning_effort=effort, env=ctx.env,
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
        available, _ = self.runner.availability()
        if not available:
            return self._unavailable()
        return self.runner.review(
            task, workspace, base_ref, verification_summary,
            timeout_minutes, model=self.config.codex.review_model,
            env=env, code_map_offered=code_map_offered,
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
        available, _ = self.runner.availability()
        if not available:
            return self._unavailable()
        return self.runner.plan_change(
            task, workspace, failure_summary, timeout_minutes,
            model=self.config.codex.planner_model, env=env,
        )
