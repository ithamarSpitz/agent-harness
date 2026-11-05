from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import yaml

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ExecutionConfig:
    max_parallel_agents: int = 3
    min_free_memory_gb: float = 2.0
    memory_per_agent_gb: float = 1.5
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
    # Per verification shell command (task, global, and stage). Kill the process
    # tree on expiry so pytest-xdist workers cannot outlive the harness.
    verification_timeout_minutes: float = 30.0
    # Keep Windows awake for the complete live run/resume lifecycle.
    keep_awake_during_run: bool = True


@dataclass(slots=True)
class WorktreeConfig:
    setup_commands: list[str] = field(default_factory=list)
    setup_timeout_minutes: int = 10


@dataclass(slots=True)
class CodeMapConfig:
    command: str = "graphify"
    build_timeout_minutes: int = 5


@dataclass(slots=True)
class CursorConfig:
    enabled: bool = True
    command: str = "agent"
    output_format: str = "stream-json"
    # Headless Cursor requires workspace trust. The harness only supplies --trust
    # for Git worktrees it created and can verify as registered worktrees.
    trust_harness_worktrees: bool = True
    models: dict[str, str | None] = field(default_factory=dict)
    # Run the `auto` model in place of the named models only while both Cursor
    # usage pools are at 100% (so Auto never spends included Cursor usage).
    auto_when_exhausted: bool = False


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
    busy_patterns: list[str] = field(default_factory=lambda: [
        "[resource_exhausted]", "resource_exhausted", "insufficient capacity",
        "provider capacity", "server overloaded", "temporarily overloaded", "high load",
    ])
    network_patterns: list[str] = field(default_factory=lambda: [
        "connection failed", "connection lost", "connection reset", "network error",
        "transport error", "websocket",
        "econnrefused", "econnreset", "etimedout", "enotfound", "eai_again",
        "getaddrinfo", "[unavailable]", "nghttp2", "stream closed",
    ])
    provider_error_patterns: list[str] = field(default_factory=lambda: [
        "service unavailable", "internal server error", "timed out", "timeout",
        "temporary failure", "502 bad gateway", "503 service unavailable", "504 gateway timeout",
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
class ClaudeReserveConfig:
    # Claude 5-hour cap: min_percent when another provider returns within
    # min_hours, max_percent from max_hours on, linear in between. The weekly
    # cap is 100 - weekly_percent_per_day x days until the weekly reset.
    min_percent: float = 60
    max_percent: float = 90
    min_hours: float = 1
    max_hours: float = 5
    weekly_percent_per_day: float = 7


@dataclass(slots=True)
class ClaudeConfig:
    enabled: bool = False
    command: str = "claude"
    implementation_sequence: list[dict[str, str]] = field(default_factory=lambda: [
        {"model": "sonnet", "effort": "medium"},
        {"model": "sonnet", "effort": "high"},
        {"model": "opus", "effort": "high"},
    ])
    review_model: str = "sonnet"
    review_effort: str = "high"
    escalation_model: str = "opus"
    escalation_effort: str = "high"
    permission_mode: str = "bypassPermissions"
    reserve: ClaudeReserveConfig = field(default_factory=ClaudeReserveConfig)


@dataclass(slots=True)
class AgyConfig:
    enabled: bool = False
    command: str = "agy"
    implementation_sequence: list[dict[str, str | None]] = field(
        default_factory=lambda: [
            {"model": "gemini-3.8-flash-medium"},
            {"model": "gemini-3.8-flash-high"},
            {"model": "claude-opus-4-6-thinking", "effort": "high"},
        ]
    )
    review_model: str = "claude-sonnet-4-6"
    review_effort: str | None = "high"
    escalation_model: str = "claude-opus-4-6-thinking"
    escalation_effort: str | None = "high"
    effort_models: list[str] = field(default_factory=lambda: ["gemini-"])
    pools: dict[str, list[str]] = field(default_factory=lambda: {
        "gemini-weekly": ["gemini-"],
        "3p-weekly": ["claude-", "gpt-oss-"],
    })
    base_allow: list[str] = field(default_factory=lambda: [
        "command(git status*)",
        "command(git diff*)",
        "command(git log*)",
        "command(git status)",
        "command(git status --short)",
        "command(git log --oneline -n 3)",
        "command(git log -n 3 --oneline)",
        "command(git log --oneline -20)",
        "command(git diff --stat)",
        "command(git show --stat HEAD)",
        "command(graphify *)",
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
        "you're out of usage",
        "you’re out of usage",
        "increase limits for faster responses",
    ])


@dataclass(slots=True)
class CapacityConfig:
    policy: str = "wait"  # wait | stop
    retry_interval_minutes: float = 5.0
    keep_awake: bool = True
    teardown_environment_while_waiting: bool = True
    busy_patterns: list[str] = field(default_factory=lambda: [
        "retriableerror: [resource_exhausted]",
        "[resource_exhausted]",
        "insufficient capacity",
        "provider capacity",
        "server overloaded",
        "temporarily overloaded",
        "high load",
    ])
    network_patterns: list[str] = field(default_factory=lambda: [
        "connection failed", "connection lost", "connection reset", "network error",
        "transport error", "websocket",
        "econnrefused", "econnreset", "etimedout", "enotfound", "eai_again",
        "getaddrinfo", "[unavailable]", "nghttp2", "stream closed",
    ])
    provider_error_patterns: list[str] = field(default_factory=lambda: [
        "service unavailable", "internal server error", "temporary failure",
        "500 internal server error", "502 bad gateway", "503 service unavailable",
        "504 gateway timeout",
    ])


@dataclass(slots=True)
class PlanRepairConfig:
    enabled: bool = True
    planner_model_class: str = "escalation"
    timeout_minutes: int = 20


@dataclass(slots=True)
class UIReviewConfig:
    enabled: bool = False
    start_commands: list[str] = field(default_factory=list)
    url: str | None = None
    ports: list[int] = field(default_factory=list)
    playwright_mcp_package: str = "@playwright/mcp@0.0.84"


@dataclass(slots=True)
class ReleaseConfig:
    commands: list[str] = field(default_factory=list)
    timeout_minutes: int = 15


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
class SelfHostingConfig:
    # When true, after a non-final stage integrates the scheduler pauses with
    # RESTART_ON_NEW_CODE and the CLI resumes from a frozen copy of that commit.
    enabled: bool = False


@dataclass(slots=True)
class HarnessConfig:
    execution: ExecutionConfig
    worktree: WorktreeConfig
    code_map: CodeMapConfig
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
    ui_review: UIReviewConfig
    release: ReleaseConfig
    doctor: DoctorConfig
    self_hosting: SelfHostingConfig = field(default_factory=SelfHostingConfig)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    agy: AgyConfig = field(default_factory=AgyConfig)

    @classmethod
    def _load_execution(cls, raw: Any) -> ExecutionConfig:
        section = dict(raw or {})
        # v0.1 exposed require_clean_base/--allow-dirty, but atomic rollback
        # fundamentally requires a clean integration checkout. Ignore the old
        # config key for backwards compatibility and always enforce cleanliness.
        section.pop("require_clean_base", None)
        config = ExecutionConfig(**section)
        if config.min_free_memory_gb <= 0:
            raise ValueError("execution.min_free_memory_gb must be greater than zero")
        if config.memory_per_agent_gb <= 0:
            raise ValueError("execution.memory_per_agent_gb must be greater than zero")
        if config.verification_timeout_minutes <= 0:
            raise ValueError(
                "execution.verification_timeout_minutes must be greater than zero"
            )
        return config

    @classmethod
    def _load_code_map(cls, raw: Any) -> CodeMapConfig:
        section = dict(raw or {})
        section.pop("enabled", None)
        section.pop("default_for_tasks", None)
        allowed = {
            key: section[key]
            for key in ("command", "build_timeout_minutes")
            if key in section
        }
        return CodeMapConfig(**allowed)

    @staticmethod
    def _load_claude(raw: Any) -> ClaudeConfig:
        section = dict(raw or {})
        reserve = dict(section.get("reserve") or {})
        for old in ("five_hour_max_used_percent", "weekly_max_used_percent"):
            if old in reserve:
                reserve.pop(old)
                LOGGER.warning(
                    "claude.reserve.%s is ignored; use claude.reserve "
                    "min_percent/max_percent/min_hours/max_hours/weekly_percent_per_day",
                    old,
                )
        section["reserve"] = ClaudeReserveConfig(**reserve)
        config = ClaudeConfig(**section)

        if not config.implementation_sequence:
            raise ValueError("claude.implementation_sequence must not be empty")

        valid_efforts = {"low", "medium", "high", "max"}
        for index, profile in enumerate(config.implementation_sequence):
            model = profile.get("model") if isinstance(profile, dict) else None
            if not isinstance(model, str) or not model.strip():
                raise ValueError(
                    f"claude.implementation_sequence[{index}].model must be a non-empty string"
                )
            effort = profile.get("effort") if isinstance(profile, dict) else None
            if not isinstance(effort, str) or effort not in valid_efforts:
                raise ValueError(
                    f"claude.implementation_sequence[{index}].effort must be one of "
                    "low, medium, high, max"
                )
        for key in ("review_effort", "escalation_effort"):
            value = getattr(config, key)
            if not isinstance(value, str) or value not in valid_efforts:
                raise ValueError(
                    f"claude.{key} must be one of low, medium, high, max"
                )

        for key in ("min_percent", "max_percent"):
            value = getattr(config.reserve, key)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not 0 <= value <= 100
            ):
                raise ValueError(f"claude.reserve.{key} must be between 0 and 100")
        for key in ("min_hours", "max_hours", "weekly_percent_per_day"):
            value = getattr(config.reserve, key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                raise ValueError(f"claude.reserve.{key} must be 0 or more")
        if config.reserve.max_percent < config.reserve.min_percent:
            raise ValueError("claude.reserve.max_percent must be at least min_percent")
        if config.reserve.max_hours <= config.reserve.min_hours:
            raise ValueError("claude.reserve.max_hours must be greater than min_hours")
        return config

    @staticmethod
    def _load_agy(raw: Any) -> AgyConfig:
        if raw is not None and not isinstance(raw, dict):
            raise ValueError("agy must be a mapping")
        config = AgyConfig(**dict(raw or {}))

        for key in (
            "command",
            "review_model",
            "escalation_model",
        ):
            value = getattr(config, key)
            if not isinstance(value, str):
                raise ValueError(f"agy.{key} must be a string")

        if not isinstance(config.implementation_sequence, list):
            raise ValueError("agy.implementation_sequence must be a list")
        if not config.implementation_sequence:
            raise ValueError("agy.implementation_sequence must not be empty")

        valid_efforts = {"low", "medium", "high", "xhigh", "max"}
        for index, profile in enumerate(config.implementation_sequence):
            key = f"agy.implementation_sequence[{index}]"
            if not isinstance(profile, dict):
                raise ValueError(f"{key} must be a mapping")
            model = profile.get("model")
            if not isinstance(model, str):
                raise ValueError(f"{key}.model must be a string")
            if "effort" in profile:
                effort = profile["effort"]
                if effort is not None and (
                    not isinstance(effort, str) or effort not in valid_efforts
                ):
                    raise ValueError(
                        f"{key}.effort must be one of low, medium, high, xhigh, max"
                    )

        for key in ("review_effort", "escalation_effort"):
            effort = getattr(config, key)
            if effort is not None and (
                not isinstance(effort, str) or effort not in valid_efforts
            ):
                raise ValueError(
                    f"agy.{key} must be one of low, medium, high, xhigh, max"
                )

        if not isinstance(config.effort_models, list):
            raise ValueError("agy.effort_models must be a list")
        for index, prefix in enumerate(config.effort_models):
            if not isinstance(prefix, str):
                raise ValueError(f"agy.effort_models[{index}] must be a string")

        if not isinstance(config.pools, dict):
            raise ValueError("agy.pools must be a mapping")
        for pool_id, prefixes in config.pools.items():
            if not isinstance(pool_id, str):
                raise ValueError("agy.pools pool ids must be strings")
            if not isinstance(prefixes, list):
                raise ValueError(f"agy.pools.{pool_id} must be a list")
            for index, prefix in enumerate(prefixes):
                if not isinstance(prefix, str):
                    raise ValueError(
                        f"agy.pools.{pool_id}[{index}] must be a string"
                    )

        if not isinstance(config.base_allow, list):
            raise ValueError("agy.base_allow must be a list")
        for index, rule in enumerate(config.base_allow):
            if not isinstance(rule, str):
                raise ValueError(f"agy.base_allow[{index}] must be a string")

        models = [
            cast(str, profile["model"])
            for profile in config.implementation_sequence
        ]
        models.extend((config.review_model, config.escalation_model))
        for model in models:
            matching_pools = {
                pool_id
                for pool_id, prefixes in config.pools.items()
                if any(model.startswith(prefix) for prefix in prefixes)
            }
            if len(matching_pools) != 1:
                raise ValueError(
                    f"agy model {model!r} must belong to exactly one pool"
                )
        return config

    @staticmethod
    def _load_wait_patterns(raw: Any, config_type: type[CodexConfig] | type[CapacityConfig]):
        section = dict(raw or {})
        defaults = config_type()
        legacy_busy = section.pop("capacity_patterns", [])
        legacy_transient = section.pop("transient_patterns", [])
        legacy_exhaustion = section.pop("exhaustion_patterns", [])
        if legacy_busy or legacy_exhaustion:
            section["busy_patterns"] = list(section.get("busy_patterns", defaults.busy_patterns)) + list(legacy_busy) + list(legacy_exhaustion)
        if legacy_transient:
            # Custom legacy transient strings have no reliable subtype. The
            # compatibility rule maps unknown transient failures to provider error.
            section["provider_error_patterns"] = list(section.get("provider_error_patterns", defaults.provider_error_patterns)) + list(legacy_transient)
        return config_type(**section)

    @classmethod
    def load(cls, path: Path) -> "HarnessConfig":
        raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return cls(
            execution=cls._load_execution(raw.get("execution", {})),
            worktree=WorktreeConfig(**raw.get("worktree", {})),
            code_map=cls._load_code_map(raw.get("code_map", {})),
            cursor=CursorConfig(**raw.get("cursor", {})),
            codex=cls._load_wait_patterns(raw.get("codex", {}), CodexConfig),
            providers=ProvidersConfig(**raw.get("providers", {})),
            retry=RetryConfig(**raw.get("retry", {})),
            verification=VerificationConfig(**raw.get("verification", {})),
            paths=PathsConfig(**raw.get("paths", {})),
            context=ContextConfig(**raw.get("context", {})),
            environment=EnvironmentConfig(**raw.get("environment", {})),
            quota=QuotaConfig(**raw.get("quota", {})),
            capacity=cls._load_wait_patterns(raw.get("capacity", {}), CapacityConfig),
            plan_repair=PlanRepairConfig(**raw.get("plan_repair", {})),
            ui_review=UIReviewConfig(**raw.get("ui_review", {})),
            release=ReleaseConfig(**raw.get("release", {})),
            doctor=DoctorConfig(**raw.get("doctor", {})),
            self_hosting=SelfHostingConfig(**raw.get("self_hosting", {})),
            claude=cls._load_claude(raw.get("claude", {})),
            agy=cls._load_agy(raw.get("agy", {})),
        )
