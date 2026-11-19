from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..agent_runner import AgentResult
from ..models import TaskSpec
from .base import BaseAdapter, CallContext, FindResult, Levels, command_version


def _label(model: str | None, auto_tier: str | None) -> str | None:
    if not model:
        return None
    return f"{model}:{auto_tier}" if auto_tier else model


class CopilotAdapter(BaseAdapter):
    name = "copilot"

    def __init__(self, runner: Any, config: Any) -> None:
        self.runner = runner
        self.config = config
        self.command = config.copilot.command

    def enabled(self) -> bool:
        return bool(self.config.copilot.enabled and self.runner is not None)

    def find(self, *, refresh: bool = False) -> FindResult:
        available, detail = self.runner.availability(refresh=refresh)
        if available:
            return FindResult(True, None, None, detail)
        found = command_version(self.config.copilot.command)
        return FindResult(found.installed, found.version, None, detail)

    def implementation_budget(self) -> int:
        return len(self.config.copilot.implementation_sequence)

    def levels(self) -> Levels:
        cfg = self.config.copilot
        sequence = cfg.implementation_sequence

        def at(index: int) -> str | None:
            if not sequence:
                return None
            profile = sequence[index]
            return _label(profile.get("model"), profile.get("auto_tier"))

        return Levels(
            worker=at(0),
            hard_worker=at(min(1, len(sequence) - 1)) if sequence else None,
            escalation=_label(cfg.escalation_model, cfg.escalation_auto_tier),
        )

    def implement(self, ctx: CallContext) -> AgentResult:
        return self.runner.implement(
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
        return self.runner.review(
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
        return self.runner.plan_change(
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
        available, _ = self.runner.availability()
        if not available:
            return AgentResult(
                False,
                "",
                "Copilot CLI/auth unavailable",
                provider="copilot",
                auth_failure=True,
            )
        return self.runner.brief_check(
            task, workspace, brief_text, timeout_minutes, env=env
        )
