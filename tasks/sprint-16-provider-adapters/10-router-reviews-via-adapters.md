---
id: S16-T10
stage: 5
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S16-T9
allowed_paths:
  - harness/provider_runner.py
  - tests/harness/test_provider_registry.py
  - tests/harness/test_provider_choice.py
  - tests/harness/test_codex_provider.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Review calls go through the adapter -->
# Goal

`_review_with_routing` calls `adapter.review(...)` for the chosen provider, with no branch on the provider name.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

The chain is in `_review_with_routing` (`harness/provider_runner.py` around lines 1270-1315), the same shape as the implementation chain S16-T9 replaced. Codex reviews pass `model=self.config.codex.review_model`; the Codex adapter (S16-T2) already does that. `_review_with_providers` (around line 1180) is the old path used only when routing is off; it stays unchanged in this task.

# Decisions (do not reopen)

1. Replace the chain with `self.adapters.get(provider).review(task, workspace, base_ref, verification_summary, timeout_minutes, env=env, code_map_offered=code_map_offered, on_process_start=on_process_start)`; a missing adapter is a launch failure `no adapter for <provider>`.
2. Disable reason: `override or result.fallback_reason or self._fallback_reason(result)`, as in S16-T9.
3. Everything after the call (fallback, waits, recording) is unchanged.

# Acceptance criteria

1. `tests/harness/test_provider_registry.py`: a review routed to a fake adapter calls its `review` once with the arguments above.
2. Codex unavailable at review disables Codex for review with reason `auth_or_cli_unavailable` and falls back, as before.
3. `grep -n 'provider == "' harness/provider_runner.py` finds no match inside `_review_with_routing`.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
