from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from .claude_runner import ClaudeAgentRunner
from .codex_runner import CodexAgentRunner
from .config import HarnessConfig
from .headroom import headroom_snapshot
from .impact import collect_outside_impact
from .logging_utils import write_log
from .models import TaskSpec, TaskRuntime, TaskStatus
from .pace import full_pace
from .prompts import outside_impact_for_review
from .runner import AgentResult, CursorAgentRunner
from .routing import Choice, adapt_headroom_snapshot, choose_provider
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
HeadroomReader = Callable[[], object]
Clock = Callable[[], float]

_CURSOR_AUTO_EXHAUSTED_UNTIL = "cursor.auto.exhausted_until"
_CURSOR_AUTO_FALLBACK_SECONDS = 24 * 60 * 60

_git_toplevel_reader: GitToplevelReader | None = None


def clear_task_quota_disables(state: StateStore, task_id: str) -> None:
    """Clear persisted quota routes for a manually reset task."""

    marker = f".{task_id}.provider."
    for key, reason in state.meta().items():
        if marker in key and isinstance(reason, str) and reason.startswith("quota"):
            state.set_meta(key, None)


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
                 cursor: CursorAgentRunner, usage: UsageRecorder | None = None,
                 claude: ClaudeAgentRunner | None = None,
                 headroom_reader: HeadroomReader | None = None,
                 clock: Clock | None = None):
        self.config = config
        self.state = state
        self.codex = codex
        self.cursor = cursor
        self.claude = claude
        self.usage = usage
        self._headroom_reader = headroom_reader or headroom_snapshot
        self._clock = clock or time.time
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

    def _claude_enabled_for(self, task: TaskSpec, phase: str | None = None) -> bool:
        return (
            self.config.claude.enabled
            and self.claude is not None
            and not bool(
                self.state.get_meta(f"{task.sprint}.{task.id}.provider.claude.disabled")
            )
            and not bool(
                phase and self.state.get_meta(
                    self._phase_disable_key(task, "claude", phase)
                )
            )
        )

    @staticmethod
    def _snapshot_has_capacity(snapshot: object, provider: str) -> bool:
        if not isinstance(snapshot, dict):
            return False
        provider_data = snapshot.get(provider)
        if not isinstance(provider_data, dict):
            return False
        windows = provider_data.get("windows")
        if not isinstance(windows, dict) or not windows:
            return False
        used_values = [
            window.get("used_percent")
            for window in windows.values()
            if isinstance(window, dict)
        ]
        return bool(used_values) and all(
            isinstance(used, (int, float)) and not isinstance(used, bool) and used < 100
            for used in used_values
        )

    def _snapshot(
        self, task: TaskSpec, *, preserve_disabled: set[str] | None = None
    ) -> dict[str, dict[str, object]]:
        try:
            snapshot = adapt_headroom_snapshot(self._headroom_reader())
        except Exception:
            snapshot = {}
        self._reconcile_cursor_auto_exhaustion(snapshot)
        for key, provider, reason in self._temporary_disable_entries(task):
            if provider not in (preserve_disabled or set()) and self._snapshot_has_capacity(
                snapshot, provider
            ):
                self.state.set_meta(key, None)
                print(f"{provider} re-enabled for {task.id} (was: {reason})", flush=True)
        return snapshot

    @staticmethod
    def _cursor_reset_at(snapshot: object) -> float | None:
        cursor = snapshot.get("cursor") if isinstance(snapshot, dict) else None
        windows = cursor.get("windows") if isinstance(cursor, dict) else None
        if not isinstance(windows, dict):
            return None
        resets: list[float] = []
        for window in windows.values():
            value = window.get("resets_at") if isinstance(window, dict) else None
            if isinstance(value, datetime):
                if value.tzinfo is None:
                    value = value.replace(tzinfo=timezone.utc)
                resets.append(value.timestamp())
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                resets.append(float(value))
        return max(resets, default=None)

    def _cursor_auto_exhausted_until(self) -> float | None:
        value = self.state.get_meta(_CURSOR_AUTO_EXHAUSTED_UNTIL)
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            if value is not None:
                self.state.set_meta(_CURSOR_AUTO_EXHAUSTED_UNTIL, None)
            return None
        exhausted_until = float(value)
        if self._clock() >= exhausted_until:
            self.state.set_meta(_CURSOR_AUTO_EXHAUSTED_UNTIL, None)
            return None
        return exhausted_until

    def _reconcile_cursor_auto_exhaustion(self, snapshot: object) -> None:
        exhausted_until = self._cursor_auto_exhausted_until()
        if exhausted_until is None:
            return
        reset_at = self._cursor_reset_at(snapshot)
        if reset_at is not None and reset_at > exhausted_until:
            # A later reset date identifies the next Cursor billing period.
            self.state.set_meta(_CURSOR_AUTO_EXHAUSTED_UNTIL, None)

    def _remember_cursor_auto_exhaustion(self, snapshot: object | None = None) -> None:
        if snapshot is None:
            try:
                snapshot = adapt_headroom_snapshot(self._headroom_reader())
            except Exception:
                snapshot = {}
        reset_at = self._cursor_reset_at(snapshot)
        now = self._clock()
        exhausted_until = (
            reset_at
            if reset_at is not None and reset_at > now
            else now + _CURSOR_AUTO_FALLBACK_SECONDS
        )
        self.state.set_meta(_CURSOR_AUTO_EXHAUSTED_UNTIL, exhausted_until)

    def _cursor_auto_out_until(self) -> float | None:
        return self._cursor_auto_exhausted_until()

    def _disabled_for(self, task: TaskSpec, phase: str) -> set[str]:
        disabled: set[str] = set()
        for provider in ("codex", "cursor", "claude"):
            if self.state.get_meta(
                f"{task.sprint}.{task.id}.provider.{provider}.disabled"
            ) or self.state.get_meta(self._phase_disable_key(task, provider, phase)):
                disabled.add(provider)
        return disabled

    def _cursor_implement_budget(self) -> int:
        return max(1, len(self.config.retry.sequence))

    @staticmethod
    def _cursor_used(snapshot: object) -> list[float | None]:
        cursor = snapshot.get("cursor") if isinstance(snapshot, dict) else None
        windows = cursor.get("windows") if isinstance(cursor, dict) else None
        used: list[float | None] = []
        for name in ("composer_grok", "other_models"):
            window = windows.get(name) if isinstance(windows, dict) else None
            value = window.get("used_percent") if isinstance(window, dict) else None
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                used.append(float(value))
            else:
                used.append(None)
        return used

    def _cursor_exhausted(self, task: TaskSpec, snapshot: object | None = None) -> bool:
        """True when auto_when_exhausted is on and both Cursor pools are known and at 100%+."""
        if not self.config.cursor.auto_when_exhausted:
            return False
        if snapshot is None:
            snapshot = self._snapshot(task)
        if self._cursor_auto_out_until() is not None:
            return False
        return all(value is not None and value >= 100 for value in self._cursor_used(snapshot))

    def _routing_active(self, task: TaskSpec, phase: str) -> bool:
        # Every project routes implementation and review through choose_provider;
        # with no headroom data the order is providers.priority exactly.
        return phase in {"implement", "review"}

    def _routing_model_class(self, task: TaskSpec, phase: str) -> str:
        # Reviews never run on Auto: "auto" makes routing drop Cursor for review.
        exhausted = self._cursor_exhausted(task)
        if phase == "review":
            return "auto" if exhausted else (task.review_model_class or "reviewer")
        if (
            phase == "implement"
            and exhausted
            and not any(self.state.get(task.id).provider_attempts.values())
        ):
            return "auto"
        return task.model_class

    def _choose(
        self,
        task: TaskSpec,
        phase: str,
        *,
        model_class: str,
        record: bool = False,
        extra_disabled: set[str] | None = None,
    ) -> Choice:
        # Claude implement/review/budget paths call _choose directly and must
        # still clear quota:* disables when leaving WAITING_QUOTA, as today.
        self._maybe_clear_temporary_disables_on_quota_wait_exit(task)
        snapshot = self._snapshot(task, preserve_disabled=extra_disabled)
        disabled = self._disabled_for(task, phase) | (extra_disabled or set())
        enabled = {
            "codex": self.config.codex.enabled,
            "cursor": self.config.cursor.enabled,
            "claude": self.config.claude.enabled and self.claude is not None,
        }
        # Auto is only the first implementation attempt of a task.
        auto_out_until = self._cursor_auto_out_until()
        auto_allowed = self.config.cursor.auto_when_exhausted and auto_out_until is None and not any(
            self.state.get(task.id).provider_attempts.values()
        )
        clock = self._clock()
        implementer = self.state.get_meta(
            f"{task.sprint}.{task.id}.provider.implementer"
        )
        choice = choose_provider(
            snapshot,
            phase=phase,
            model_class=model_class,
            enabled=enabled,
            priority=self.config.providers.priority,
            implementer_provider=implementer if isinstance(implementer, str) else None,
            disabled_for_task=disabled,
            claude_reserve=self.config.claude.reserve,
            now=datetime.fromtimestamp(clock, tz=timezone.utc),
            pace=lambda provider, window: full_pace(provider, window, now=clock),
            auto_when_exhausted=auto_allowed,
        )
        if record:
            message = f"provider choice for {phase}: {choice.provider or 'none'} ({choice.reason})"
            if (
                choice.provider == "cursor"
                and self.config.cursor.auto_when_exhausted
                and phase in {"implement", "review"}
            ):
                if phase == "implement" and choice.auto:
                    message += "; cursor: auto (both pools at 100%)"
                else:
                    cls = (
                        (task.review_model_class or "reviewer") if phase == "review"
                        else self._cursor_model_class(task)
                    )
                    message += f"; cursor: {self.config.cursor.models.get(cls) or cls}"
            if auto_out_until is not None:
                out_date = datetime.fromtimestamp(auto_out_until, tz=timezone.utc).date()
                message += f"; cursor auto: out until {out_date.isoformat()}"
            log_dir = getattr(self.codex, "log_dir", None)
            if log_dir is not None:
                write_log(log_dir, f"{task.id}-routing.log", message + "\n")
            print(f"{task.id}: {message}", flush=True)
            # The board reads this key; usage.jsonl holds provider calls only.
            self.state.set_meta(
                f"{task.sprint}.{task.id}.board.routing_reason", choice.reason
            )
        return choice

    def _has_other_candidate(
        self, task: TaskSpec, phase: str, provider: str, unavailable: set[str]
    ) -> bool:
        """True when another provider is enabled and not disabled for this task."""

        skipped = self._disabled_for(task, phase) | unavailable
        enabled = {
            "codex": self.config.codex.enabled,
            "cursor": self.config.cursor.enabled,
            "claude": self.config.claude.enabled and self.claude is not None,
        }
        return any(
            on and name not in skipped for name, on in enabled.items() if name != provider
        )

    def _record_switch(self, task: TaskSpec, provider: str, reason: str, switch_to: str) -> None:
        if self.usage is not None:
            self.usage.record({
                "event_type": "provider_switch", "provider": provider, "switch_to": switch_to,
                "task_id": task.id, "reason": reason, "ok": False, "duration_seconds": 0.0,
                "code_map": False,
            })

    def _disable_provider(self, task: TaskSpec, provider: str, reason: str) -> None:
        self.state.set_meta(
            f"{task.sprint}.{task.id}.provider.{provider}.disabled", reason
        )

    @classmethod
    def _fallback_reason(cls, result: AgentResult) -> str:
        return cls._codex_fallback_reason(result)

    def _preferred_provider(self, task: TaskSpec, phase: str) -> str:
        self._maybe_clear_temporary_disables_on_quota_wait_exit(task)
        # Quota disables must clear from live headroom even when Claude routing
        # is off (e.g. WAITING_TIMEOUT resume after a 5-hour window reset).
        self._snapshot(task)
        if self._routing_active(task, phase):
            return self._choose(
                task, phase, model_class=self._routing_model_class(task, phase)
            ).provider or "none"
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
        if self.config.claude.enabled or self._routing_active(task, phase):
            self.state.set_meta(self._phase_disable_key(task, provider, phase), "stalled")
            next_provider = self._choose(
                task, phase, model_class=self._routing_model_class(task, phase)
            ).provider
            if next_provider == "codex":
                try:
                    codex_available, _ = self.codex.availability(refresh=True)
                except Exception:
                    codex_available = False
                if not codex_available:
                    next_provider = None
            if next_provider is None:
                self.state.set_meta(self._phase_disable_key(task, provider, phase), None)
                return False
            if self.usage is not None:
                self.usage.record({
                    "event_type": "provider_switch", "provider": provider,
                    "switch_to": next_provider, "task_id": task.id,
                    "reason": f"stalled:{phase}", "ok": False,
                    "duration_seconds": 0.0, "code_map": False,
                })
            return True
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
        claude_attempts = (
            len(self.config.claude.implementation_sequence)
            if self.config.claude.enabled else 0
        )
        return (
            len(self.config.codex.implementation_sequence)
            + self._cursor_implement_budget()
            + claude_attempts
        )

    def has_implementation_budget(self, task: TaskSpec, *, model_class_override: str | None = None) -> bool:
        if task.id in self._worktree_guard_blocked:
            return False
        if model_class_override is not None:
            if not self.config.cursor.enabled:
                return False
            return self._provider_attempt(task, "cursor") < 1
        if self._routing_active(task, "implement"):
            exhausted: set[str] = set()
            if self._provider_attempt(task, "codex") >= len(
                self.config.codex.implementation_sequence
            ):
                exhausted.add("codex")
            if self._provider_attempt(task, "cursor") >= self._cursor_implement_budget():
                exhausted.add("cursor")
            if self._provider_attempt(task, "claude") >= len(
                self.config.claude.implementation_sequence
            ):
                exhausted.add("claude")
            return self._choose(
                task, "implement",
                model_class=self._routing_model_class(task, "implement"),
                extra_disabled=exhausted,
            ).provider is not None
        provider = self.active_provider(task)
        if provider == "codex":
            return self._provider_attempt(task, "codex") < len(self.config.codex.implementation_sequence)
        if provider == "cursor":
            return self._provider_attempt(task, "cursor") < max(1, len(self.config.retry.sequence))
        return False

    def _cursor_model_class(self, task: TaskSpec, *, model_class_override: str | None = None) -> str:
        if model_class_override is not None:
            return model_class_override
        if self._cursor_exhausted(task):
            return "auto"
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

    @staticmethod
    def _track_agent_process(
        on_process_start: Callable[[int], None] | None,
    ) -> tuple[Callable[[int], None], dict[str, float | int]]:
        """Wrap the PID hook to remember the agent PID and its Unix start time."""
        seen: dict[str, float | int] = {}

        def hook(pid: int) -> None:
            seen["pid"] = pid
            seen["started_at"] = time.time()
            if on_process_start is not None:
                on_process_start(pid)

        return hook, seen

    def _safe_ui_review_cleanup(
        self,
        *,
        task: TaskSpec,
        workspace: Path,
        ports_before: dict[int, str],
        agent_pid: int | None = None,
        agent_started_at: float | None = None,
    ) -> None:
        """Stop new ui_review listeners / worktree processes; never raise to the caller."""
        try:
            cleanup_ui_review_processes(
                config=self.config.ui_review,
                worktree=workspace,
                log_dir=self.codex.log_dir,
                task_id=task.id,
                ports_before=ports_before,
                agent_pid=agent_pid,
                agent_started_at=agent_started_at,
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
        hook, seen = self._track_agent_process(on_process_start)
        try:
            result = self._implement_inner(
                task, workspace, timeout_minutes, attempt, previous_failure,
                env=env, model_class_override=model_class_override,
                code_map_offered=code_map_offered,
                on_process_start=hook,
            )
            if result.ok and result.provider in {"codex", "cursor", "claude"}:
                # Every path records the implementer so review can avoid it.
                self.state.set_meta(
                    f"{task.sprint}.{task.id}.provider.implementer", result.provider
                )
            return result
        finally:
            if ui_enabled:
                self._safe_ui_review_cleanup(
                    task=task, workspace=workspace, ports_before=ports_before,
                    agent_pid=cast("int | None", seen.get("pid")),
                    agent_started_at=seen.get("started_at"),
                )

    def _implement_inner(self, task: TaskSpec, workspace: Path, timeout_minutes: int, attempt: int,
                         previous_failure: str | None, *, env: dict[str, str] | None = None,
                         model_class_override: str | None = None,
                         code_map_offered: bool = False,
                         on_process_start: Callable[[int], None] | None = None) -> AgentResult:
        if model_class_override is None and self._routing_active(task, "implement"):
            return self._implement_with_routing(
                task, workspace, timeout_minutes, attempt, previous_failure,
                env=env, code_map_offered=code_map_offered,
                on_process_start=on_process_start,
            )
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
        if model_class == "auto" and result.quota_exhausted:
            self._remember_cursor_auto_exhaustion()
        if self._should_consume_cursor_attempt(result):
            self._consume_provider_attempt(task, "cursor")
        return result

    def _implement_with_routing(
        self,
        task: TaskSpec,
        workspace: Path,
        timeout_minutes: int,
        attempt: int,
        previous_failure: str | None,
        *,
        env: dict[str, str] | None,
        code_map_offered: bool,
        on_process_start: Callable[[int], None] | None,
    ) -> AgentResult:
        unavailable: set[str] = set()
        last_result: AgentResult | None = None
        switched: tuple[str, str] | None = None
        override: str | None = None
        while True:
            exhausted = set(unavailable)
            routing_class = self._routing_model_class(task, "implement")
            if self._provider_attempt(task, "codex") >= len(
                self.config.codex.implementation_sequence
            ):
                exhausted.add("codex")
            if self._provider_attempt(task, "cursor") >= self._cursor_implement_budget():
                exhausted.add("cursor")
            if self._provider_attempt(task, "claude") >= len(
                self.config.claude.implementation_sequence
            ):
                exhausted.add("claude")
            choice = self._choose(
                task, "implement", model_class=routing_class,
                record=True, extra_disabled=exhausted,
            )
            provider = choice.provider
            if provider is None:
                return last_result or AgentResult(
                    False, "", choice.reason, provider="none", quota_exhausted=True
                )
            if switched is not None:
                self._record_switch(task, switched[0], switched[1], provider)
                switched = None
            override = None
            cursor_model_class: str | None = None
            try:
                if provider == "codex":
                    available, _ = self.codex.availability()
                    if not available:
                        override = "auth_or_cli_unavailable"
                        result = AgentResult(
                            False, "", "Codex CLI/auth unavailable",
                            provider="codex", auth_failure=True,
                        )
                    else:
                        index = self._provider_attempt(task, "codex")
                        model, effort = self.config.codex.implementation_profile(index)
                        result = self.codex.implement(
                            task, workspace, timeout_minutes, attempt, previous_failure,
                            model=model, reasoning_effort=effort, env=env,
                            code_map_offered=code_map_offered,
                            on_process_start=on_process_start,
                        )
                elif provider == "claude" and self.claude is not None:
                    result = self.claude.implement(
                        task, workspace, timeout_minutes, attempt, previous_failure,
                        env=env, code_map_offered=code_map_offered,
                        on_process_start=on_process_start,
                    )
                else:
                    cursor_model_class = self._cursor_model_class(task)
                    result = self.cursor.implement(
                        task, workspace, timeout_minutes, attempt, previous_failure,
                        env=env, model_class_override=cursor_model_class,
                        code_map_offered=code_map_offered,
                        on_process_start=on_process_start,
                    )
            except Exception as exc:
                override = f"integration_error:{type(exc).__name__}"
                result = AgentResult(
                    False, "", f"{provider} integration error: {type(exc).__name__}",
                    provider=provider, launch_failure=True,
                )

            if (
                provider == "cursor"
                and cursor_model_class == "auto"
                and result.quota_exhausted
            ):
                self._remember_cursor_auto_exhaustion()

            if self._provider_unavailable(result) or self._is_launch_failure(result):
                last_result = result
                if not self._has_other_candidate(task, "implement", provider, unavailable):
                    return result
                reason = override or self._fallback_reason(result)
                self._disable_provider(task, provider, reason)
                switched = (provider, reason)
                unavailable.add(provider)
                continue
            if result.capacity_exhausted or result.transient_error:
                return result
            if provider != "cursor" or self._should_consume_cursor_attempt(result):
                self._consume_provider_attempt(task, provider)
            if result.ok:
                self.state.set_meta(
                    f"{task.sprint}.{task.id}.provider.implementer", provider
                )
            return result

    def review(self, task: TaskSpec, workspace: Path, base_ref: str, verification_summary: str,
               timeout_minutes: int, *, env: dict[str, str] | None = None,
               code_map_offered: bool = False,
               on_process_start: Callable[[int], None] | None = None) -> AgentResult:
        guarded = self._guard_workspace(task, workspace)
        if guarded is not None:
            return guarded
        ui_enabled = self.config.ui_review.enabled
        snapshot = capture_worktree_snapshot(workspace)
        ports_before = snapshot_port_listeners(self.config.ui_review.ports) if ui_enabled else {}
        hook, seen = self._track_agent_process(on_process_start)
        try:
            result = self._review_inner(
                task, workspace, base_ref, verification_summary, timeout_minutes,
                env=env, code_map_offered=code_map_offered,
                on_process_start=hook,
            )
            after = capture_worktree_snapshot(workspace)
            changed = worktree_changed_paths(snapshot, after)
            if changed:
                result = cast(
                    AgentResult, fail_review_for_worktree_edits(result, changed)
                )
            return result
        finally:
            if ui_enabled:
                self._safe_ui_review_cleanup(
                    task=task, workspace=workspace, ports_before=ports_before,
                    agent_pid=cast("int | None", seen.get("pid")),
                    agent_started_at=seen.get("started_at"),
                )

    def _review_inner(self, task: TaskSpec, workspace: Path, base_ref: str, verification_summary: str,
                      timeout_minutes: int, *, env: dict[str, str] | None = None,
                      code_map_offered: bool = False,
                      on_process_start: Callable[[int], None] | None = None) -> AgentResult:
        impact_body = collect_outside_impact(workspace, base_ref)
        with outside_impact_for_review(impact_body):
            return self._review_with_providers(
                task, workspace, base_ref, verification_summary, timeout_minutes,
                env=env, code_map_offered=code_map_offered,
                on_process_start=on_process_start,
            )

    def _review_with_providers(
        self,
        task: TaskSpec,
        workspace: Path,
        base_ref: str,
        verification_summary: str,
        timeout_minutes: int,
        *,
        env: dict[str, str] | None = None,
        code_map_offered: bool = False,
        on_process_start: Callable[[int], None] | None = None,
    ) -> AgentResult:
        if self._routing_active(task, "review"):
            return self._review_with_routing(
                task, workspace, base_ref, verification_summary, timeout_minutes,
                env=env, code_map_offered=code_map_offered,
                on_process_start=on_process_start,
            )
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

    def _review_with_routing(
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
        unavailable: set[str] = set()
        last_result: AgentResult | None = None
        switched: tuple[str, str] | None = None
        override: str | None = None
        while True:
            choice = self._choose(
                task, "review",
                model_class=self._routing_model_class(task, "review"),
                record=True, extra_disabled=unavailable,
            )
            provider = choice.provider
            if provider is None:
                return last_result or AgentResult(
                    False, "", choice.reason, provider="none", quota_exhausted=True
                )
            if switched is not None:
                self._record_switch(task, switched[0], switched[1], provider)
                switched = None
            override = None
            try:
                if provider == "codex":
                    available, _ = self.codex.availability()
                    if not available:
                        override = "auth_or_cli_unavailable"
                        result = AgentResult(
                            False, "", "Codex CLI/auth unavailable",
                            provider="codex", auth_failure=True,
                        )
                    else:
                        result = self.codex.review(
                            task, workspace, base_ref, verification_summary,
                            timeout_minutes, model=self.config.codex.review_model,
                            env=env, code_map_offered=code_map_offered,
                            on_process_start=on_process_start,
                        )
                elif provider == "claude" and self.claude is not None:
                    result = self.claude.review(
                        task, workspace, base_ref, verification_summary,
                        timeout_minutes, env=env,
                        code_map_offered=code_map_offered,
                        on_process_start=on_process_start,
                    )
                else:
                    result = self.cursor.review(
                        task, workspace, base_ref, verification_summary,
                        timeout_minutes, env=env,
                        code_map_offered=code_map_offered,
                        on_process_start=on_process_start,
                    )
            except Exception as exc:
                override = f"integration_error:{type(exc).__name__}"
                result = AgentResult(
                    False, "", f"{provider} integration error: {type(exc).__name__}",
                    provider=provider, launch_failure=True,
                )
            if self._provider_unavailable(result) or self._is_launch_failure(result):
                last_result = result
                if not self._has_other_candidate(task, "review", provider, unavailable):
                    return result
                reason = override or self._fallback_reason(result)
                self._disable_provider(task, provider, reason)
                switched = (provider, reason)
                unavailable.add(provider)
                continue
            return result

    def plan_change(self, task: TaskSpec, workspace: Path, failure_summary: str, timeout_minutes: int,
                    *, model_class: str, env: dict[str, str] | None = None) -> AgentResult:
        guarded = self._guard_workspace(task, workspace)
        if guarded is not None:
            return guarded
        if self.config.claude.enabled:
            return self._plan_change_with_routing(
                task, workspace, failure_summary, timeout_minutes,
                model_class=model_class, env=env,
            )
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

    def _plan_change_with_routing(
        self,
        task: TaskSpec,
        workspace: Path,
        failure_summary: str,
        timeout_minutes: int,
        *,
        model_class: str,
        env: dict[str, str] | None,
    ) -> AgentResult:
        unavailable: set[str] = set()
        last_result: AgentResult | None = None
        while True:
            choice = self._choose(
                task, "plan_repair", model_class=model_class,
                record=True, extra_disabled=unavailable,
            )
            provider = choice.provider
            if provider is None:
                return last_result or AgentResult(
                    False, "", choice.reason, provider="none", quota_exhausted=True
                )
            try:
                if provider == "codex":
                    available, _ = self.codex.availability()
                    if not available:
                        result = AgentResult(
                            False, "", "Codex CLI/auth unavailable",
                            provider="codex", auth_failure=True,
                        )
                    else:
                        result = self.codex.plan_change(
                            task, workspace, failure_summary, timeout_minutes,
                            model=self.config.codex.planner_model, env=env,
                        )
                elif provider == "claude" and self.claude is not None:
                    result = self.claude.plan_change(
                        task, workspace, failure_summary, timeout_minutes, env=env
                    )
                else:
                    result = self.cursor.plan_change(
                        task, workspace, failure_summary, timeout_minutes,
                        model_class=model_class, env=env,
                    )
            except Exception as exc:
                result = AgentResult(
                    False, "", f"{provider} integration error: {type(exc).__name__}",
                    provider=provider, launch_failure=True,
                )
            if self._provider_unavailable(result) or self._is_launch_failure(result):
                last_result = result
                self._disable_provider(task, provider, self._fallback_reason(result))
                unavailable.add(provider)
                continue
            return result
