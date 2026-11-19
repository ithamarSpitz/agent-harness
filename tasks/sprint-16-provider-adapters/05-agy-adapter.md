---
id: S16-T5
stage: 2
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on:
  - S16-T1
allowed_paths:
  - harness/adapters/agy.py
  - tests/harness/test_adapter_agy.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- agy adapter -->
# Goal

`AgyAdapter` in `harness/adapters/agy.py` wraps `AgyAgentRunner` behind the adapter protocol.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

`harness/adapters/base.py` (S16-T1) defines `ProviderAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault` and `classify_result`; read it and implement that protocol exactly. Each adapter receives its runner and config in its constructor and only delegates: the arguments it passes to the runner must be exactly the ones `ProviderAgentRunner` passes today at the line ranges named below, so moving the call into the adapter changes nothing. `plan_change` receives `model_class`: forward it as `model_class=` where the runner takes it today (agy, Copilot, Cursor); Claude's runner takes no model class, and Codex passes `model=config.codex.planner_model` instead. Plan-repair calls get no `on_process_start`.

Today in `harness/provider_runner.py`: implementation around lines 1043-1052 (`agy.implement(task, workspace, timeout_minutes, provider_attempt + 1, previous_failure, env=, model_class_override=routing_class, code_map_offered=, exhausted_pools=self._agy_exhausted_pools(task), on_process_start=)`); review around lines 1273-1279; plan_change around lines 1406-1409. `_agy_enabled_for` (around line 246) requires `config.agy.enabled` and a runner. agy is turned off in this repo's `harness.yaml`; do not turn it on.

# Decisions (do not reopen)

1. `name = "agy"`. Constructor takes `config`, `runner: AgyAgentRunner | None`, and `exhausted_pools_for(task) -> set[str]` from the router. `enabled()` = `config.agy.enabled and runner is not None`.
2. `implement(ctx)` passes `ctx.provider_attempt + 1` as the attempt and `ctx.model_class` as `model_class_override`, as today.
3. `find()` = `command_version(config.agy.command)`.
4. `levels()`: worker = `implementation_sequence[0]["model"]`, hard_worker = the model at index `min(1, len-1)`, escalation = `config.agy.escalation_model`.
5. `implementation_budget()` = `len(config.agy.implementation_sequence)`.

# Acceptance criteria

1. `tests/harness/test_adapter_agy.py` with a fake runner: implement passes attempt `provider_attempt + 1`, the routing class and the pools from `exhausted_pools_for`; review and plan_change pass today's arguments.
2. `enabled()` is false with no runner.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
