from __future__ import annotations

import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Protocol

from ..agent_runner import AgentResult
from ..models import TaskSpec


@dataclass(slots=True)
class FindResult:
    installed: bool
    version: str | None
    signed_in: bool | None
    detail: str


@dataclass(slots=True)
class Levels:
    worker: str | None
    hard_worker: str | None
    escalation: str | None


class Fault(str, Enum):
    OK = "ok"
    TASK = "task"
    QUOTA = "quota"
    AUTH = "auth"
    MODEL_UNAVAILABLE = "model_unavailable"
    CONFIG = "config"
    LAUNCH = "launch"
    CAPACITY = "capacity"
    TRANSIENT = "transient"


def classify_result(result: AgentResult) -> Fault:
    if result.ok:
        return Fault.OK
    if result.quota_exhausted:
        return Fault.QUOTA
    if result.auth_failure:
        return Fault.AUTH
    if result.model_unavailable:
        return Fault.MODEL_UNAVAILABLE
    if result.config_fault:
        return Fault.CONFIG
    if getattr(result, "launch_failure", False) or getattr(result, "resource_launch_failure", False):
        return Fault.LAUNCH
    if result.capacity_exhausted:
        return Fault.CAPACITY
    if result.transient_error or result.waiting_cause is not None:
        return Fault.TRANSIENT
    return Fault.TASK


@dataclass(slots=True)
class CallContext:
    task: TaskSpec
    workspace: Path
    timeout_minutes: int
    attempt: int
    provider_attempt: int
    previous_failure: str | None
    env: dict[str, str] | None
    model_class: str
    code_map_offered: bool
    on_process_start: Callable[[int], None] | None


class ProviderAdapter(Protocol):
    name: str

    def enabled(self) -> bool: ...

    def find(self, *, refresh: bool = False) -> FindResult: ...

    def implementation_budget(self) -> int: ...

    def levels(self) -> Levels: ...

    def implement(self, ctx: CallContext) -> AgentResult: ...

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
    ) -> AgentResult: ...

    def plan_change(
        self,
        task: TaskSpec,
        workspace: Path,
        failure_summary: str,
        timeout_minutes: int,
        *,
        model_class: str,
        env: dict[str, str] | None,
    ) -> AgentResult: ...

    def classify(self, result: AgentResult) -> Fault: ...


class BaseAdapter:
    command: str = ""

    def classify(self, result: AgentResult) -> Fault:
        return classify_result(result)

    def find(self, *, refresh: bool = False) -> FindResult:
        return command_version(self.command)

    def enabled(self) -> bool:
        raise NotImplementedError

    def implementation_budget(self) -> int:
        raise NotImplementedError

    def levels(self) -> Levels:
        raise NotImplementedError

    def implement(self, ctx: CallContext) -> AgentResult:
        raise NotImplementedError

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
        raise NotImplementedError

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
        raise NotImplementedError


def command_version(command: str) -> FindResult:
    try:
        completed = subprocess.run(
            [command, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return FindResult(False, None, None, str(error))

    output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if completed.returncode == 0:
        return FindResult(True, first_line or None, None, "")
    return FindResult(True, None, None, first_line)
