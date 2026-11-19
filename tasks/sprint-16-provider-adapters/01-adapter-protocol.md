---
id: S16-T1
stage: 1
model_class: hard_worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/adapters/__init__.py
  - harness/adapters/base.py
  - harness/agent_runner.py
  - tests/harness/test_adapters_base.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Adapter protocol, shared types and registry -->
# Goal

A new package `harness/adapters/` defines the one shape every provider adapter has, a default failure classifier, and a registry. Nothing uses it yet.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

Failure flags live on `AgentResult` (`harness/agent_runner.py`): `quota_exhausted`, `capacity_exhausted`, `transient_error`, `waiting_cause`, `auth_failure`, `model_unavailable`, `config_fault` (added in S15-T2), and `launch_failure` / `resource_launch_failure`, which exist only on the subclass in `harness/runner.py` and are read with `getattr(..., False)` in `provider_runner.py` (around lines 809-845). Today's fallback reason text comes from `ProviderAgentRunner._codex_fallback_reason` (around line 825): `quota:<scope>`, `auth`, `model_unavailable`, `launch_failure`. Codex sets a special reason `auth_or_cli_unavailable` when `availability()` fails (around line 1070), through a local `override` variable.

# Decisions (do not reopen)

1. `harness/adapters/base.py` contains:
   - `FindResult` dataclass: `installed: bool`, `version: str | None`, `signed_in: bool | None` (None = unknown), `detail: str`.
   - `Levels` dataclass: `worker: str | None`, `hard_worker: str | None`, `escalation: str | None` (model names).
   - `Fault` enum (str values): `OK`, `TASK`, `QUOTA`, `AUTH`, `MODEL_UNAVAILABLE`, `CONFIG`, `LAUNCH`, `CAPACITY`, `TRANSIENT`.
   - `classify_result(result) -> Fault` with this precedence: `ok` -> OK; `quota_exhausted` -> QUOTA; `auth_failure` -> AUTH; `model_unavailable` -> MODEL_UNAVAILABLE; `config_fault` -> CONFIG; `launch_failure` or `resource_launch_failure` (via getattr) -> LAUNCH; `capacity_exhausted` -> CAPACITY; `transient_error` or `waiting_cause` not None -> TRANSIENT; else TASK.
   - `CallContext` dataclass for an implementation call: `task`, `workspace`, `timeout_minutes`, `attempt` (scheduler attempt), `provider_attempt` (0-based count of this provider's earlier steps on the task), `previous_failure`, `env`, `model_class` (routing class), `code_map_offered`, `on_process_start`.
   - `ProviderAdapter` Protocol with: `name: str`; `enabled() -> bool` (config flag and runner present); `find(*, refresh: bool = False) -> FindResult`; `implementation_budget() -> int`; `levels() -> Levels`; `implement(ctx: CallContext) -> AgentResult`; `review(task, workspace, base_ref, verification_summary, timeout_minutes, *, env, code_map_offered, on_process_start) -> AgentResult`; `plan_change(task, workspace, failure_summary, timeout_minutes, *, model_class: str, env) -> AgentResult` (the router passes its routing class; today agy, Copilot and Cursor runners take `model_class=`, Claude's runner takes none, Codex takes `model=config.codex.planner_model`; around lines 1405-1440 of `provider_runner.py`); `classify(result) -> Fault`.
   - `BaseAdapter` class with a `command: str` attribute, implementing only `classify` (with `classify_result`) and `find` (with `command_version(self.command)`); its other protocol methods raise `NotImplementedError`.
   - Module function `command_version(command) -> FindResult`: runs `<command> --version` (15 s timeout, never raises). Not found or OSError: `installed=False`, `version=None`, `detail=str(error)`. Exit 0: `installed=True`, `version` = first non-empty output line stripped. Nonzero exit: `installed=True`, `version=None`, `detail` = first non-empty output line. Always `signed_in=None`.
2. `AgentResult` gets `fallback_reason: str | None = None`: an adapter sets it when the router must record a specific disable reason (Codex: `auth_or_cli_unavailable`). Nothing reads it yet.
3. `harness/adapters/__init__.py`: `AdapterRegistry` with `register(adapter)` (duplicate name raises ValueError), `get(name) -> ProviderAdapter | None`, `names()` (registration order), `enabled()` (adapters whose `enabled()` is true, registration order), `__iter__`. Also re-export the base types.
4. No other module imports `harness.adapters` in this task.

# Acceptance criteria

1. `tests/harness/test_adapters_base.py`: `classify_result` returns each Fault for a result with only that flag set, and follows the precedence for a result with quota and launch flags both set (QUOTA).
2. A fake adapter subclassing `BaseAdapter` registers; a second with the same name raises ValueError; `names()` keeps order; `enabled()` skips a disabled one.
3. `command_version` with a missing command returns `installed=False` without raising (use a name that cannot exist).

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
