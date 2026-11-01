from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .models import TaskSpec


@dataclass(slots=True)
class AgentResult:
    """Provider-neutral result consumed by scheduler/state/usage logic."""

    ok: bool
    output: str
    error: str | None = None
    timed_out: bool = False
    stalled: bool = False
    duration_seconds: float = 0.0
    model: str | None = None
    quota_exhausted: bool = False
    quota_scope: str | None = None
    capacity_exhausted: bool = False
    transient_error: bool = False
    auth_failure: bool = False
    model_unavailable: bool = False
    provider: str = "cursor"
    input_tokens: int | None = None
    cached_input_tokens: int | None = None
    output_tokens: int | None = None
    reasoning_output_tokens: int | None = None


class AgentRunner(Protocol):
    """Common provider execution contract.

    Both Cursor and Codex run inside the supplied task worktree and return the
    same structured ``AgentResult``. Provider-specific model selection is
    intentionally left to the small routing layer rather than the scheduler.
    """

    def implement(
        self,
        task: TaskSpec,
        workspace: Path,
        timeout_minutes: int,
        attempt: int,
        previous_failure: str | None,
        **kwargs,
    ) -> AgentResult: ...

    def review(
        self,
        task: TaskSpec,
        workspace: Path,
        base_ref: str,
        verification_summary: str,
        timeout_minutes: int,
        **kwargs,
    ) -> AgentResult: ...

    def plan_change(
        self,
        task: TaskSpec,
        workspace: Path,
        failure_summary: str,
        timeout_minutes: int,
        **kwargs,
    ) -> AgentResult: ...
