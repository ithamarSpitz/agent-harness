from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(slots=True)
class ExecutionConfig:
    max_parallel_agents: int = 3
    default_max_attempts: int = 2
    default_timeout_minutes: int = 35
    # Optional start-point guard. On the first run of a sprint, the current
    # integration checkout must resolve to this ref. Resumes continue from the
    # recorded integration branch after prior stages have been merged.
    base_ref: str | None = None
    keep_successful_worktrees: bool = False
    # Watchdog is independent of the hard timeout. It kills an agent only when
    # both CLI output and repository file activity have been silent this long.
    stall_timeout_minutes: float = 12.0
    watchdog_poll_seconds: float = 10.0
    # Keep Windows awake for the complete live run/resume lifecycle.
    keep_awake_during_run: bool = True


@dataclass(slots=True)
class WorktreeConfig:
    setup_commands: list[str] = field(default_factory=list)
    setup_timeout_minutes: int = 10


@dataclass(slots=True)
class CursorConfig:
    enabled: bool = True
    command: str = "agent"
    output_format: str = "text"
    # Headless Cursor requires workspace trust. The harness only supplies --trust
    # for Git worktrees it created and can verify as registered worktrees.
    trust_harness_worktrees: bool = True
    models: dict[str, str | None] = field(default_factory=dict)


@dataclass(slots=True)
class CodexConfig:
    enabled: bool = False
    command: str = "codex"
    reasoning_effort: str = "high"
    sandbox: str = "workspace-write"
    # Native Windows Codex workspace-write currently has ACL/read-back regressions.
    # When set, implementation calls on Windows use this explicit sandbox instead.
    # Reviews/plan-repair remain read-only.
    windows_implementation_sandbox: str | None = None
    approval_policy: str = "never"
    network_access: bool = True
    # Each implementation attempt can pin both a model and a reasoning effort.
    # String entries from v0.12.4 remain supported and inherit reasoning_effort.
    implementation_sequence: list[str | dict[str, str]] = field(default_factory=lambda: [
        {"model": "gpt-5.6-sol", "reasoning_effort": "medium"},
        {"model": "gpt-5.6-sol", "reasoning_effort": "medium"},
        {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
        {"model": "gpt-6-astra", "reasoning_effort": "medium"},
    ])
    review_model: str = "gpt-5.6-sol"
    planner_model: str = "gpt-5.6-sol"

    def implementation_profile(self, index: int) -> tuple[str, str]:
        entry = self.implementation_sequence[index]
        if isinstance(entry, str):
            model = entry.strip()
            effort = self.reasoning_effort.strip()
        elif isinstance(entry, dict):
            model = str(entry.get("model") or "").strip()
            effort = str(entry.get("reasoning_effort") or self.reasoning_effort).strip()
        else:
            raise TypeError(f"Unsupported Codex implementation profile at index {index}: {entry!r}")
        if not model:
            raise ValueError(f"Codex implementation profile {index + 1} is missing model")
        if not effort:
            raise ValueError(f"Codex implementation profile {index + 1} is missing reasoning_effort")
        return model, effort

    auth_required_substring: str = "Logged in using ChatGPT"
    short_quota_patterns: list[str] = field(default_factory=lambda: [
        "5h limit exhausted", "5-hour limit exhausted", "5 hour limit exhausted",
        "5h usage limit", "5-hour usage limit", "5 hour usage limit",
    ])
    weekly_quota_patterns: list[str] = field(default_factory=lambda: [
        "weekly limit exhausted", "weekly usage limit exhausted", "weekly usage limit",
    ])
    quota_patterns: list[str] = field(default_factory=lambda: [
        "you've hit your usage limit", "you have hit your usage limit",
        "usage limit reached", "no codex usage remaining",
    ])
    capacity_patterns: list[str] = field(default_factory=lambda: [
        "[resource_exhausted]", "resource_exhausted", "insufficient capacity",
        "provider capacity", "server overloaded", "temporarily overloaded", "high load",
    ])
    transient_patterns: list[str] = field(default_factory=lambda: [
        "connection lost", "connection reset", "network error", "transport error",
        "websocket", "service unavailable", "internal server error", "timed out",
        "timeout", "temporary failure", "502 bad gateway", "503 service unavailable",
        "504 gateway timeout",
    ])
    auth_patterns: list[str] = field(default_factory=lambda: [
        "not logged in", "please run codex login", "authentication required",
        "unauthorized", "invalid authentication", "invalid token", "401",
    ])
    model_unavailable_patterns: list[str] = field(default_factory=lambda: [
        "model is not available", "model unavailable", "unsupported model",
        "unknown model", "model not found", "does not have access to model",
        "model is not supported", "model isn't supported",
        "not supported when using codex with a chatgpt account",
    ])


@dataclass(slots=True)
class ProvidersConfig:
    priority: list[str] = field(default_factory=lambda: ["codex", "cursor"])


@dataclass(slots=True)
class RetryConfig:
    # Automatic implementation-attempt routing. When empty, the task's
    # model_class is reused for the configured attempt budget.
    sequence: list[str] = field(default_factory=list)


@dataclass(slots=True)
class VerificationConfig:
    global_task_commands: list[str] = field(default_factory=lambda: ["git diff --check"])
    stage_commands: list[str] = field(default_factory=list)
    # Fallback environment used only by verification subprocesses. Values from
    # the caller/task environment win, so runtime credentials are never replaced.
    env: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class PathsConfig:
    protected: list[str] = field(default_factory=list)
    # Touching any of these paths forces an independent review even when the
    # task itself has review: false.
    review_required: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ContextConfig:
    aliases: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class EnvironmentConfig:
    # Docker management is opt-in per task via `environment: docker`.
    docker_enabled: bool = False
    compose_command: str = "docker compose"
    compose_files: list[str] = field(default_factory=lambda: ["docker-compose.yml"])
    project_name_prefix: str = "harness"
    services: list[str] = field(default_factory=list)
    validate_compose: bool = True
    up_before_agent: bool = False
    teardown_on_success: bool = True
    teardown_on_blocked: bool = True
    remove_volumes: bool = True
    command_timeout_minutes: int = 10
    env: dict[str, str] = field(default_factory=dict)



@dataclass(slots=True)
class QuotaConfig:
    policy: str = "wait"  # wait | stop
    reset_at: str | None = None
    probe_interval_minutes: float = 60.0
    reset_grace_minutes: float = 5.0
    keep_awake: bool = True
    teardown_environment_while_waiting: bool = True
    exhaustion_patterns: list[str] = field(default_factory=lambda: [
        "usage limit reached",
        "you've hit your usage limit",
        "you have hit your usage limit",
        "out of included usage",
        "monthly usage limit",
        "quota exceeded",
        "spending limit reached",
        "no usage remaining",
    ])


@dataclass(slots=True)
class CapacityConfig:
    policy: str = "wait"  # wait | stop
    retry_interval_minutes: float = 5.0
    keep_awake: bool = True
    teardown_environment_while_waiting: bool = True
    exhaustion_patterns: list[str] = field(default_factory=lambda: [
        "retriableerror: [resource_exhausted]",
        "[resource_exhausted]",
        "insufficient capacity",
        "provider capacity",
        "server overloaded",
        "temporarily overloaded",
        "high load",
    ])


@dataclass(slots=True)
class PlanRepairConfig:
    enabled: bool = True
    planner_model_class: str = "escalation"
    timeout_minutes: int = 20


@dataclass(slots=True)
class DoctorConfig:
    # Project-specific commands/env are deliberately empty in the generic
    # starter. The NYC repo can later declare node/docker and required env vars.
    required_commands: list[str] = field(default_factory=list)
    required_env: list[str] = field(default_factory=list)
    min_free_disk_gb: float = 1.0
    check_cursor_models: bool = True
    minimum_versions: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class HarnessConfig:
    execution: ExecutionConfig
    worktree: WorktreeConfig
    cursor: CursorConfig
    codex: CodexConfig
    providers: ProvidersConfig
    retry: RetryConfig
    verification: VerificationConfig
    paths: PathsConfig
    context: ContextConfig
    environment: EnvironmentConfig
    quota: QuotaConfig
    capacity: CapacityConfig
    plan_repair: PlanRepairConfig
    doctor: DoctorConfig

    @classmethod
    def load(cls, path: Path) -> "HarnessConfig":
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        execution_raw = dict(raw.get("execution", {}))
        # v0.1 exposed require_clean_base/--allow-dirty, but atomic rollback
        # fundamentally requires a clean integration checkout. Ignore the old
        # config key for backwards compatibility and always enforce cleanliness.
        execution_raw.pop("require_clean_base", None)
        return cls(
            execution=ExecutionConfig(**execution_raw),
            worktree=WorktreeConfig(**raw.get("worktree", {})),
            cursor=CursorConfig(**raw.get("cursor", {})),
            codex=CodexConfig(**raw.get("codex", {})),
            providers=ProvidersConfig(**raw.get("providers", {})),
            retry=RetryConfig(**raw.get("retry", {})),
            verification=VerificationConfig(**raw.get("verification", {})),
            paths=PathsConfig(**raw.get("paths", {})),
            context=ContextConfig(**raw.get("context", {})),
            environment=EnvironmentConfig(**raw.get("environment", {})),
            quota=QuotaConfig(**raw.get("quota", {})),
            capacity=CapacityConfig(**raw.get("capacity", {})),
            plan_repair=PlanRepairConfig(**raw.get("plan_repair", {})),
            doctor=DoctorConfig(**raw.get("doctor", {})),
        )
