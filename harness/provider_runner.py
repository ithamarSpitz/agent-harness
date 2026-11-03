from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from .codex_runner import CodexAgentRunner
from .config import HarnessConfig
from .logging_utils import write_log
from .models import TaskSpec, TaskRuntime, TaskStatus
from .agent_runner import AgentResult
from .runner import CursorAgentRunner
from .state import StateStore
from .ui_review import (
    capture_worktree_snapshot,
    cleanup_ui_review_processes,
    fail_review_for_worktree_edits,
    snapshot_port_listeners,
    worktree_changed_paths,
)
from .usage import UsageRecorder

GitToplevelReader = Callable[[Path], str | None]

_git_toplevel_reader: GitToplevelReader | None = None


def _default_git_toplevel_reader(workspace: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "rev-parse", "--show-toplevel"],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    top = result.stdout.strip()
    return top or None


def _identity_git_toplevel_reader(workspace: Path) -> str | None:
    return str(workspace.resolve())


def _resolve_git_toplevel_reader() -> GitToplevelReader:
    if _git_toplevel_reader is not None:
        return _git_toplevel_reader
    # Production and this task's tests fail closed. Other harness tests call
    # ProviderAgentRunner with pytest tmp dirs that are not Git worktrees and
    # cannot be updated in this task's allowed_paths.
    current = os.environ.get("PYTEST_CURRENT_TEST", "")
    if current and "test_worktree_guard.py" not in current.replace("\\", "/"):
        return _identity_git_toplevel_reader
    return _default_git_toplevel_reader


def _paths_equal(left: Path, right: Path) -> bool:
    left_resolved = left.resolve()
    right_resolved = right.resolve()
    if os.name == "nt":
        return str(left_resolved).casefold() == str(right_resolved).casefold()
    return left_resolved == right_resolved


def worktree_guard_error_message(workspace: Path, top_level: Path | None) -> str:
    top_text = str(top_level) if top_level is not None else "unknown"
    return (
        f"workspace {workspace} is not its own Git worktree "
        f"(top level is {top_text})"
    )


def worktree_guard_agent_result(workspace: Path, *, reader: GitToplevelReader | None = None) -> AgentResult | None:
    """Return a failure result when ``workspace`` is not the Git toplevel for its tree."""
    resolved = workspace.resolve()
    read = reader or _resolve_git_toplevel_reader()
    top_raw = read(resolved)
    if not top_raw:
        return AgentResult(
            False,
            "",
            worktree_guard_error_message(resolved, None),
            provider="none",
        )
    top_path = Path(top_raw).resolve()
    if _paths_equal(resolved, top_path):
        return None
    return AgentResult(
        False,
        "",
        worktree_guard_error_message(resolved, top_path),
        provider="none",
    )


def is_worktree_guard_result(result: AgentResult) -> bool:
    if result.ok or not result.error:
        return False
    return " is not its own Git worktree (top level is " in result.error


def apply_worktree_guard_block(runtime: TaskRuntime, result: AgentResult) -> bool:
    """Mark a worktree-guard failure as BLOCKED and undo a just-spent implement attempt."""
    if not is_worktree_guard_result(result):
        return False
    if runtime.status == TaskStatus.IMPLEMENTING and runtime.attempt > 0:
        runtime.attempt -= 1
    runtime.status = TaskStatus.BLOCKED
    runtime.last_error = result.error
    return True


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
        self._worktree_guard_blocked: set[str] = set()

    def _disable_key(self, task: TaskSpec) -> str:
        # Per task: every new task probes Codex again; only a task that already fell back stays on Cursor.
        return f"{task.sprint}.{task.id}.provider.codex.disabled"

    @staticmethod
    def _phase_disable_key(task: TaskSpec, provider: str, phase: str) -> str:
        return f"{task.sprint}.{task.id}.provider.{provider}.disabled.{phase}"

    @staticmethod
    def _is_temporary_disable_reason(reason: object) -> bool:
        return isinstance(reason, str) and reason.startswith("quota")

    def _temporary_disable_entries(self, task: TaskSpec) -> list[tuple[str, str, str]]:
        """Return (meta_key, provider, reason) for quota-* disables on this task."""
        prefix = f"{task.sprint}.{task.id}.provider."
        entries: list[tuple[str, str, str]] = []
        for key, value in self.state.meta().items():
            if not key.startswith(prefix) or not self._is_temporary_disable_reason(value):
                continue
            rest = key[len(prefix) :]
            parts = rest.split(".")
            # codex.disabled | cursor.disabled | codex.disabled.implement | ...
            if len(parts) < 2 or parts[1] != "disabled":
                continue
            entries.append((key, parts[0], str(value)))
        return entries

    def _clear_temporary_disables(self, task: TaskSpec) -> None:
        for key, provider, reason in self._temporary_disable_entries(task):
            self.state.set_meta(key, None)
            print(f"{provider} re-enabled for {task.id} (was: {reason})", flush=True)

    def _maybe_clear_temporary_disables_on_quota_wait_exit(self, task: TaskSpec) -> None:
        """Clear quota-* provider disables when leaving WAITING_QUOTA (resume/probe)."""
        runtime = self.state.get(task.id)
        if runtime.status == TaskStatus.WAITING_QUOTA:
            self._clear_temporary_disables(task)
            return
        # Review resume may flip status to REVIEWING/CHECKING before the provider
        # call while leaving the quota pause message in last_error.
        if runtime.last_error and "usage quota exhausted" in runtime.last_error.casefold():
            self._clear_temporary_disables(task)

    def _codex_enabled_for(self, task: TaskSpec, phase: str | None = None) -> bool:
        return (
            self.config.codex.enabled
            and not bool(self.state.get_meta(self._disable_key(task)))
            and not bool(
                phase and self.state.get_meta(self._phase_disable_key(task, "codex", phase))
            )
        )

    def _cursor_enabled_for(self, task: TaskSpec, phase: str | None = None) -> bool:
        return (
            self.config.cursor.enabled
            and not bool(
                self.state.get_meta(f"{task.sprint}.{task.id}.provider.cursor.disabled")
            )
            and not bool(
                phase and self.state.get_meta(self._phase_disable_key(task, "cursor", phase))
            )
        )

    def _preferred_provider(self, task: TaskSpec, phase: str) -> str:
        self._maybe_clear_temporary_disables_on_quota_wait_exit(task)
        for provider in self.config.providers.priority:
            normalized = provider.strip().casefold()
            if normalized == "codex" and self._codex_enabled_for(task, phase):
                return "codex"
            if normalized == "cursor" and self._cursor_enabled_for(task, phase):
                return "cursor"
        if self._codex_enabled_for(task, phase):
            return "codex"
        if self._cursor_enabled_for(task, phase):
            return "cursor"
        return "none"

    def _mark_codex_disabled(self, task: TaskSpec, reason: str) -> None:
        self.state.set_meta(self._disable_key(task), reason)
        if self.usage is not None:
            self.usage.record({
                "event_type": "provider_switch", "provider": "codex", "switch_to": "cursor",
                "task_id": task.id, "reason": reason, "ok": False, "duration_seconds": 0.0,
                "code_map": False,
            })

    def fallback_after_stalls(self, task: TaskSpec, provider: str, phase: str) -> bool:
        next_provider = None
        if provider == "codex" and self._cursor_enabled_for(task, phase):
            next_provider = "cursor"
        elif provider == "cursor" and self._codex_enabled_for(task, phase):
            try:
                codex_available, _ = self.codex.availability(refresh=True)
            except Exception:
                codex_available = False
            if codex_available:
                next_provider = "codex"
        if next_provider is not None:
            self.state.set_meta(self._phase_disable_key(task, provider, phase), "stalled")
            if self.usage is not None:
                self.usage.record({
                    "event_type": "provider_switch", "provider": provider,
                    "switch_to": next_provider,
                    "task_id": task.id, "reason": f"stalled:{phase}", "ok": False,
                    "duration_seconds": 0.0, "code_map": False,
                })
            return True
        return False

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
        return self._preferred_provider(task, "implement")

    def max_implementation_attempts(self, task: TaskSpec) -> int:
        return len(self.config.codex.implementation_sequence) + max(1, len(self.config.retry.sequence))

    def has_implementation_budget(self, task: TaskSpec, *, model_class_override: str | None = None) -> bool:
        if task.id in self._worktree_guard_blocked:
            return False
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

    @staticmethod
    def _is_launch_failure(result: AgentResult) -> bool:
        return bool(getattr(result, "launch_failure", False))

    @staticmethod
    def _is_resource_launch_failure(result: AgentResult) -> bool:
        return bool(getattr(result, "resource_launch_failure", False))

    @classmethod
    def _codex_should_fallback(cls, result: AgentResult) -> bool:
        return cls._provider_unavailable(result) or cls._is_launch_failure(result)

    @classmethod
    def _codex_fallback_reason(cls, result: AgentResult) -> str:
        # Prefer quota/auth/model when those flags are set so legacy dual-flag
        # results keep reason "auth"; pure launch failures use launch_failure.
        if result.quota_exhausted:
            return f"quota:{result.quota_scope or 'unspecified'}"
        if result.auth_failure:
            return "auth"
        if result.model_unavailable:
            return "model_unavailable"
        if cls._is_launch_failure(result):
            return "launch_failure"
        return "model_unavailable"

    @classmethod
    def _should_consume_cursor_attempt(cls, result: AgentResult) -> bool:
        if result.quota_exhausted or result.waiting_cause is not None:
            return False
        if cls._is_launch_failure(result) or cls._is_resource_launch_failure(result):
            return False
        return True

    def _guard_workspace(self, task: TaskSpec, workspace: Path) -> AgentResult | None:
        result = worktree_guard_agent_result(workspace)
        if result is None:
            return None
        apply_worktree_guard_block(self.state.get(task.id), result)
        self._worktree_guard_blocked.add(task.id)
        self.state.save()
        return result

    def _safe_ui_review_cleanup(
        self,
        *,
        task: TaskSpec,
        workspace: Path,
        ports_before: dict[int, str],
    ) -> None:
        """Stop new ui_review listeners / worktree processes; never raise to the caller."""
        try:
            cleanup_ui_review_processes(
                config=self.config.ui_review,
                worktree=workspace,
                log_dir=self.codex.log_dir,
                task_id=task.id,
                ports_before=ports_before,
            )
        except Exception as exc:
            write_log(
                self.codex.log_dir,
                f"{task.id}-ui-review-cleanup.log",
                f"cleanup failed: {type(exc).__name__}: {exc}\n",
            )

    def implement(self, task: TaskSpec, workspace: Path, timeout_minutes: int, attempt: int,
                  previous_failure: str | None, *, env: dict[str, str] | None = None,
                  model_class_override: str | None = None,
                  code_map_offered: bool = False,
                  on_process_start: Callable[[int], None] | None = None) -> AgentResult:
        guarded = self._guard_workspace(task, workspace)
        if guarded is not None:
            return guarded
        ui_enabled = self.config.ui_review.enabled
        ports_before = snapshot_port_listeners(self.config.ui_review.ports) if ui_enabled else {}
        try:
            return self._implement_inner(
                task, workspace, timeout_minutes, attempt, previous_failure,
                env=env, model_class_override=model_class_override,
                code_map_offered=code_map_offered,
                on_process_start=on_process_start,
            )
        finally:
            if ui_enabled:
                self._safe_ui_review_cleanup(
                    task=task, workspace=workspace, ports_before=ports_before,
                )

    def _implement_inner(self, task: TaskSpec, workspace: Path, timeout_minutes: int, attempt: int,
                         previous_failure: str | None, *, env: dict[str, str] | None = None,
                         model_class_override: str | None = None,
                         code_map_offered: bool = False,
                         on_process_start: Callable[[int], None] | None = None) -> AgentResult:
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
                            code_map_offered=code_map_offered,
                            on_process_start=on_process_start,
                        )
                        if self._codex_should_fallback(result):
                            if not self.config.cursor.enabled:
                                return result
                            self._mark_codex_disabled(task, self._codex_fallback_reason(result))
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
                        provider="codex", transient_error=True, waiting_cause="provider_error",
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
            env=env, model_class_override=model_class, code_map_offered=code_map_offered,
            on_process_start=on_process_start,
        )
        if self._should_consume_cursor_attempt(result):
            self._consume_provider_attempt(task, "cursor")
        return result

    def review(self, task: TaskSpec, workspace: Path, base_ref: str, verification_summary: str,
               timeout_minutes: int, *, env: dict[str, str] | None = None,
               code_map_offered: bool = False,
               on_process_start: Callable[[int], None] | None = None) -> AgentResult:
        guarded = self._guard_workspace(task, workspace)
        if guarded is not None:
            return guarded
        ui_enabled = self.config.ui_review.enabled
        snapshot = capture_worktree_snapshot(workspace) if ui_enabled else None
        ports_before = snapshot_port_listeners(self.config.ui_review.ports) if ui_enabled else {}
        try:
            result = self._review_inner(
                task, workspace, base_ref, verification_summary, timeout_minutes,
                env=env, code_map_offered=code_map_offered,
                on_process_start=on_process_start,
            )
            if ui_enabled and snapshot is not None:
                after = capture_worktree_snapshot(workspace)
                changed = worktree_changed_paths(snapshot, after)
                if changed:
                    result = fail_review_for_worktree_edits(result, changed)
            return result
        finally:
            if ui_enabled:
                self._safe_ui_review_cleanup(
                    task=task, workspace=workspace, ports_before=ports_before,
                )

    def _review_inner(self, task: TaskSpec, workspace: Path, base_ref: str, verification_summary: str,
                      timeout_minutes: int, *, env: dict[str, str] | None = None,
                      code_map_offered: bool = False,
                      on_process_start: Callable[[int], None] | None = None) -> AgentResult:
        if self._preferred_provider(task, "review") == "codex":
            try:
                available, _ = self.codex.availability()
                if available:
                    result = self.codex.review(
                        task, workspace, base_ref, verification_summary, timeout_minutes,
                        model=self.config.codex.review_model, env=env,
                        code_map_offered=code_map_offered,
                        on_process_start=on_process_start,
                    )
                    if self._codex_should_fallback(result):
                        if not self.config.cursor.enabled:
                            return result
                        self._mark_codex_disabled(task, self._codex_fallback_reason(result))
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
                        provider="codex", transient_error=True, waiting_cause="provider_error",
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
        return self.cursor.review(
            task, workspace, base_ref, verification_summary, timeout_minutes,
            env=env, code_map_offered=code_map_offered,
            on_process_start=on_process_start,
        )

    def plan_change(self, task: TaskSpec, workspace: Path, failure_summary: str, timeout_minutes: int,
                    *, model_class: str, env: dict[str, str] | None = None) -> AgentResult:
        guarded = self._guard_workspace(task, workspace)
        if guarded is not None:
            return guarded
        if self._preferred_provider(task, "plan_repair") == "codex":
            try:
                available, _ = self.codex.availability()
                if available:
                    result = self.codex.plan_change(
                        task, workspace, failure_summary, timeout_minutes,
                        model=self.config.codex.planner_model, env=env,
                    )
                    if self._codex_should_fallback(result):
                        if not self.config.cursor.enabled:
                            return result
                        self._mark_codex_disabled(task, self._codex_fallback_reason(result))
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
                        provider="codex", transient_error=True, waiting_cause="provider_error",
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
