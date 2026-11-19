from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from ..agent_runner import AgentResult
from ..config import HarnessConfig
from ..models import TaskSpec
from ..runner import CursorAgentRunner
from .base import BaseAdapter, CallContext, FindResult, Levels, command_version


class CursorAdapter(BaseAdapter):
    name = "cursor"

    def __init__(
        self,
        runner: CursorAgentRunner,
        config: HarnessConfig,
        *,
        model_class_for: Callable[[TaskSpec], str],
        on_auto_quota: Callable[[], None],
        budget: Callable[[], int],
    ) -> None:
        self.runner = runner
        self.config = config
        self.command = config.cursor.command
        self._model_class_for = model_class_for
        self._on_auto_quota = on_auto_quota
        self._budget = budget

    def enabled(self) -> bool:
        return bool(self.config.cursor.enabled)

    def find(self, *, refresh: bool = False) -> FindResult:
        return command_version(self.config.cursor.command)

    def implementation_budget(self) -> int:
        return self._budget()

    def levels(self) -> Levels:
        models = self.config.cursor.models
        return Levels(
            worker=models.get("worker"),
            hard_worker=models.get("hard_worker"),
            escalation=models.get("escalation"),
        )

    def implement(self, ctx: CallContext) -> AgentResult:
        model_class = self._model_class_for(ctx.task)
        result = self.runner.implement(
            ctx.task, ctx.workspace, ctx.timeout_minutes, ctx.attempt,
            ctx.previous_failure,
            env=ctx.env, model_class_override=model_class,
            code_map_offered=ctx.code_map_offered,
            on_process_start=ctx.on_process_start,
        )
        if model_class == "auto" and result.quota_exhausted:
            self._on_auto_quota()
        return result

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
        return self.runner.review(
            task, workspace, base_ref, verification_summary, timeout_minutes,
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
        return self.runner.plan_change(
            task, workspace, failure_summary, timeout_minutes,
            model_class=model_class, env=env,
        )

    def consumes_attempt(self, result: AgentResult) -> bool:
        if result.quota_exhausted or result.waiting_cause is not None:
            return False
        if getattr(result, "launch_failure", False) or getattr(result, "resource_launch_failure", False):
            return False
        return True
