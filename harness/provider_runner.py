from __future__ import annotations

from pathlib import Path

from .codex_runner import CodexAgentRunner
from .config import HarnessConfig
from .models import TaskSpec
from .agent_runner import AgentResult
from .runner import CursorAgentRunner
from .state import StateStore
from .usage import UsageRecorder


class ProviderAgentRunner:
    """Provider router with optional Cursor fallback."""

    manages_model_routing = True

    def __init__(self, config: HarnessConfig, state: StateStore, codex: CodexAgentRunner,
                 cursor: CursorAgentRunner, usage: UsageRecorder | None = None):
        self.config = config
        self.state = state
        self.codex = codex
        self.cursor = cursor
        self.usage = usage

    def _disable_key(self, task: TaskSpec) -> str:
        # Per task: every new task probes Codex again; only a task that already fell back stays on Cursor.
        return f"{task.sprint}.{task.id}.provider.codex.disabled"

    def _codex_enabled_for(self, task: TaskSpec) -> bool:
        return (
            self.config.codex.enabled
            and not bool(self.state.get_meta(self._disable_key(task)))
        )

    def _preferred_provider(self, task: TaskSpec) -> str:
        for provider in self.config.providers.priority:
            normalized = provider.strip().casefold()
            if normalized == "codex" and self._codex_enabled_for(task):
                return "codex"
            if normalized == "cursor" and self.config.cursor.enabled:
                return "cursor"
        if self._codex_enabled_for(task):
            return "codex"
        if self.config.cursor.enabled:
            return "cursor"
        return "none"

    def _mark_codex_disabled(self, task: TaskSpec, reason: str) -> None:
        self.state.set_meta(self._disable_key(task), reason)
        if self.usage is not None:
            self.usage.record({
                "event_type": "provider_switch", "provider": "codex", "switch_to": "cursor",
                "task_id": task.id, "reason": reason, "ok": False, "duration_seconds": 0.0,
            })

    def _provider_attempt(self, task: TaskSpec, provider: str) -> int:
        return int(self.state.get(task.id).provider_attempts.get(provider, 0))

    def _consume_provider_attempt(self, task: TaskSpec, provider: str) -> None:
        runtime = self.state.get(task.id)
        runtime.provider_attempts[provider] = self._provider_attempt(task, provider) + 1
        self.state.save()

    def active_provider(self, task: TaskSpec, *, model_class_override: str | None = None) -> str:
        if model_class_override is not None:
            if not self.config.cursor.enabled:
                raise RuntimeError("Cursor is disabled in harness.yaml; remove --model-class or explicitly re-enable Cursor")
            return "cursor"
        return self._preferred_provider(task)

    def max_implementation_attempts(self, task: TaskSpec) -> int:
        return len(self.config.codex.implementation_sequence) + max(1, len(self.config.retry.sequence))

    def has_implementation_budget(self, task: TaskSpec, *, model_class_override: str | None = None) -> bool:
        if model_class_override is not None:
            if not self.config.cursor.enabled:
                return False
            return self._provider_attempt(task, "cursor") < 1
        provider = self.active_provider(task)
        if provider == "codex":
            return self._provider_attempt(task, "codex") < len(self.config.codex.implementation_sequence)
        if provider == "cursor":
            return self._provider_attempt(task, "cursor") < max(1, len(self.config.retry.sequence))
        return False

    def _cursor_model_class(self, task: TaskSpec, *, model_class_override: str | None = None) -> str:
        if model_class_override is not None:
            return model_class_override
        sequence = self.config.retry.sequence or [task.model_class]
        index = self._provider_attempt(task, "cursor")
        return sequence[min(index, len(sequence) - 1)]

    @staticmethod
    def _provider_unavailable(result: AgentResult) -> bool:
        return result.quota_exhausted or result.auth_failure or result.model_unavailable

    def implement(self, task: TaskSpec, workspace: Path, timeout_minutes: int, attempt: int,
                  previous_failure: str | None, *, env: dict[str, str] | None = None,
                  model_class_override: str | None = None) -> AgentResult:
        if self.active_provider(task, model_class_override=model_class_override) == "codex":
            try:
                available, _ = self.codex.availability()
                if available:
                    index = self._provider_attempt(task, "codex")
                    if index < len(self.config.codex.implementation_sequence):
                        model, reasoning_effort = self.config.codex.implementation_profile(index)
                        result = self.codex.implement(
                            task, workspace, timeout_minutes, attempt, previous_failure,
                            model=model, reasoning_effort=reasoning_effort, env=env,
                        )
                        if self._provider_unavailable(result):
                            if not self.config.cursor.enabled:
                                return result
                            reason = (f"quota:{result.quota_scope or 'unspecified'}" if result.quota_exhausted else ("auth" if result.auth_failure else "model_unavailable"))
                            self._mark_codex_disabled(task, reason)
                        elif result.capacity_exhausted or result.transient_error:
                            return result
                        else:
                            self._consume_provider_attempt(task, "codex")
                            return result
                else:
                    if not self.config.cursor.enabled:
                        return AgentResult(
                            False, "", "Codex CLI/auth unavailable and Cursor fallback is disabled",
                            provider="codex", auth_failure=True,
                        )
                    self._mark_codex_disabled(task, "auth_or_cli_unavailable")
            except Exception as exc:
                if not self.config.cursor.enabled:
                    return AgentResult(
                        False, "", f"Codex integration error: {type(exc).__name__}",
                        provider="codex", transient_error=True,
                    )
                self._mark_codex_disabled(task, f"integration_error:{type(exc).__name__}")

        if not self.config.cursor.enabled:
            return AgentResult(
                False,
                "",
                "Codex is unavailable and Cursor fallback is disabled in harness.yaml",
                provider="codex",
                auth_failure=True,
            )

        model_class = self._cursor_model_class(task, model_class_override=model_class_override)
        result = self.cursor.implement(
            task, workspace, timeout_minutes, attempt, previous_failure,
            env=env, model_class_override=model_class,
        )
        if not result.quota_exhausted and not result.capacity_exhausted:
            self._consume_provider_attempt(task, "cursor")
        return result

    def review(self, task: TaskSpec, workspace: Path, base_ref: str, verification_summary: str,
               timeout_minutes: int, *, env: dict[str, str] | None = None) -> AgentResult:
        if self._preferred_provider(task) == "codex":
            try:
                available, _ = self.codex.availability()
                if available:
                    result = self.codex.review(
                        task, workspace, base_ref, verification_summary, timeout_minutes,
                        model=self.config.codex.review_model, env=env,
                    )
                    if self._provider_unavailable(result):
                        if not self.config.cursor.enabled:
                            return result
                        reason = (f"quota:{result.quota_scope or 'unspecified'}" if result.quota_exhausted else ("auth" if result.auth_failure else "model_unavailable"))
                        self._mark_codex_disabled(task, reason)
                    else:
                        return result
                else:
                    if not self.config.cursor.enabled:
                        return AgentResult(
                            False, "", "Codex CLI/auth unavailable and Cursor fallback is disabled",
                            provider="codex", auth_failure=True,
                        )
                    self._mark_codex_disabled(task, "auth_or_cli_unavailable")
            except Exception as exc:
                if not self.config.cursor.enabled:
                    return AgentResult(
                        False, "", f"Codex integration error: {type(exc).__name__}",
                        provider="codex", transient_error=True,
                    )
                self._mark_codex_disabled(task, f"integration_error:{type(exc).__name__}")
        if not self.config.cursor.enabled:
            return AgentResult(
                False,
                "",
                "Codex review is unavailable and Cursor fallback is disabled in harness.yaml",
                provider="codex",
                auth_failure=True,
            )
        return self.cursor.review(task, workspace, base_ref, verification_summary, timeout_minutes, env=env)

    def plan_change(self, task: TaskSpec, workspace: Path, failure_summary: str, timeout_minutes: int,
                    *, model_class: str, env: dict[str, str] | None = None) -> AgentResult:
        if self._preferred_provider(task) == "codex":
            try:
                available, _ = self.codex.availability()
                if available:
                    result = self.codex.plan_change(
                        task, workspace, failure_summary, timeout_minutes,
                        model=self.config.codex.planner_model, env=env,
                    )
                    if self._provider_unavailable(result):
                        if not self.config.cursor.enabled:
                            return result
                        reason = (f"quota:{result.quota_scope or 'unspecified'}" if result.quota_exhausted else ("auth" if result.auth_failure else "model_unavailable"))
                        self._mark_codex_disabled(task, reason)
                    else:
                        return result
                else:
                    if not self.config.cursor.enabled:
                        return AgentResult(
                            False, "", "Codex CLI/auth unavailable and Cursor fallback is disabled",
                            provider="codex", auth_failure=True,
                        )
                    self._mark_codex_disabled(task, "auth_or_cli_unavailable")
            except Exception as exc:
                if not self.config.cursor.enabled:
                    return AgentResult(
                        False, "", f"Codex integration error: {type(exc).__name__}",
                        provider="codex", transient_error=True,
                    )
                self._mark_codex_disabled(task, f"integration_error:{type(exc).__name__}")
        if not self.config.cursor.enabled:
            return AgentResult(
                False,
                "",
                "Codex plan repair is unavailable and Cursor fallback is disabled in harness.yaml",
                provider="codex",
                auth_failure=True,
            )
        return self.cursor.plan_change(
            task, workspace, failure_summary, timeout_minutes, model_class=model_class, env=env
        )
