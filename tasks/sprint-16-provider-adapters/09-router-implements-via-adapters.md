---
id: S16-T9
stage: 4
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S16-T8
allowed_paths:
  - harness/provider_runner.py
  - tests/harness/test_provider_registry.py
  - tests/harness/test_provider_choice.py
  - tests/harness/test_codex_provider.py
  - tests/harness/test_copilot_wiring.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Implementation calls go through the adapter -->
# Goal

`_implement_with_routing` calls `adapter.implement(CallContext(...))` for whichever provider routing chose, with no branch on the provider name.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

The if/elif chain is in `_implement_with_routing` (`harness/provider_runner.py` around lines 1040-1095). After the call it disables the provider on `_provider_unavailable(result) or _is_launch_failure(result)` with reason `override or _fallback_reason(result)`, returns on capacity/transient, handles S15-T2's `config_fault`, consumes an attempt (Cursor only when `_should_consume_cursor_attempt`), and records the implementer.

# Decisions (do not reopen)

1. Replace the chain with `adapter = self.adapters.get(provider)` and `adapter.implement(CallContext(task=..., workspace=..., timeout_minutes=..., attempt=attempt, provider_attempt=self._provider_attempt(task, provider), previous_failure=..., env=env, model_class=routing_class, code_map_offered=..., on_process_start=...))`. A missing adapter is a launch failure with error `no adapter for <provider>`.
2. Disable reason: `override or result.fallback_reason or self._fallback_reason(result)`; the `override` variable stays only for the integration-error case.
3. Attempt consumption: `consumes = getattr(adapter, "consumes_attempt", None)`; consume when it is None or returns true. The Cursor Auto bookkeeping now happens inside the Cursor adapter (S16-T4), so remove it here.
4. The fault tests after the call stay as they are (do not switch to `classify` yet); the order of checks must not change.

# Acceptance criteria

1. `tests/harness/test_provider_registry.py`: a fake adapter's `implement` receives a `CallContext` with `provider_attempt` 0 then 1 on two failed tries.
2. Codex unavailable during implementation disables Codex with reason `auth_or_cli_unavailable`, as before.
3. Cursor Auto exhaustion is remembered after an Auto quota result (existing test, or a new one if none exists).
4. `grep -n 'provider == "' harness/provider_runner.py` finds no match inside `_implement_with_routing`.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
