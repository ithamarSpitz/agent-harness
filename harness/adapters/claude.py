from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Callable

from ..agent_runner import AgentResult
from ..config import HarnessConfig
from ..models import TaskSpec
from .base import BaseAdapter, CallContext, FindResult, Levels, command_version

if TYPE_CHECKING:
    from ..claude_runner import ClaudeAgentRunner


class ClaudeAdapter(BaseAdapter):
    name = "claude"

    def __init__(self, config: HarnessConfig, runner: ClaudeAgentRunner | None) -> None:
        self.config = config
        self.runner = runner
        self.command = config.claude.command

    def enabled(self) -> bool:
        return self.config.claude.enabled and self.runner is not None

    def find(self, *, refresh: bool = False) -> FindResult:
        return command_version(self.config.claude.command)

    def implementation_budget(self) -> int:
        return len(self.config.claude.implementation_sequence)

    def levels(self) -> Levels:
        sequence = self.config.claude.implementation_sequence
        worker = sequence[0]["model"] if sequence else None
        hard = sequence[min(1, len(sequence) - 1)]["model"] if sequence else None
        return Levels(worker, hard, self.config.claude.escalation_model)

    def implement(self, ctx: CallContext) -> AgentResult:
        assert self.runner is not None
        return self.runner.implement(
            ctx.task, ctx.workspace, ctx.timeout_minutes, ctx.attempt,
            ctx.previous_failure,
            env=ctx.env, code_map_offered=ctx.code_map_offered,
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
        assert self.runner is not None
        return self.runner.review(
            task, workspace, base_ref, verification_summary,
            timeout_minutes, env=env,
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
        assert self.runner is not None
        return self.runner.plan_change(
            task, workspace, failure_summary, timeout_minutes, env=env
        )
