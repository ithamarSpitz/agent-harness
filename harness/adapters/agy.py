from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..agent_runner import AgentResult
from ..agy_runner import AgyAgentRunner
from ..config import HarnessConfig
from ..models import TaskSpec
from .base import BaseAdapter, CallContext, FindResult, Levels, command_version


class AgyAdapter(BaseAdapter):
    name = "agy"

    def __init__(
        self,
        config: HarnessConfig,
        runner: AgyAgentRunner | None,
        exhausted_pools_for: Callable[[TaskSpec], set[str]],
    ) -> None:
        self.config = config
        self.runner = runner
        self.exhausted_pools_for = exhausted_pools_for

    @property
    def command(self) -> str:  # type: ignore[override]
        return self.config.agy.command

    def enabled(self) -> bool:
        return self.config.agy.enabled and self.runner is not None

    def find(self, *, refresh: bool = False) -> FindResult:
        return command_version(self.config.agy.command)

    def implementation_budget(self) -> int:
        return len(self.config.agy.implementation_sequence)

    def levels(self) -> Levels:
        sequence = self.config.agy.implementation_sequence
        worker = sequence[0]["model"] if sequence else None
        hard = sequence[min(1, len(sequence) - 1)]["model"] if sequence else None
        return Levels(
            worker=worker,
            hard_worker=hard,
            escalation=self.config.agy.escalation_model,
        )

    def _require(self) -> AgyAgentRunner:
        if self.runner is None:
            raise RuntimeError("agy runner is not available")
        return self.runner

    def implement(self, ctx: CallContext) -> AgentResult:
        return self._require().implement(
            ctx.task,
            ctx.workspace,
            ctx.timeout_minutes,
            ctx.provider_attempt + 1,
            ctx.previous_failure,
            env=ctx.env,
            model_class_override=ctx.model_class,
            code_map_offered=ctx.code_map_offered,
            exhausted_pools=self.exhausted_pools_for(ctx.task),
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
        return self._require().review(
            task, workspace, base_ref, verification_summary, timeout_minutes,
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
        return self._require().plan_change(
            task, workspace, failure_summary, timeout_minutes,
            model_class=model_class, env=env,
        )
