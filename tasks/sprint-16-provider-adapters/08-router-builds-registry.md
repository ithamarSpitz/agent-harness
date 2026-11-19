---
id: S16-T8
stage: 3
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S16-T2
  - S16-T3
  - S16-T4
  - S16-T5
  - S16-T6
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

<!-- Router builds the adapter registry and uses it for enabled sets and budgets -->
# Goal

`ProviderAgentRunner` builds an `AdapterRegistry` of the five adapters and uses it wherever it builds an `enabled` map or a per-provider attempt limit, instead of naming each provider.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

The same five-entry `enabled` dict is written out three times in `harness/provider_runner.py`: in `_choose` (around line 530), `_has_other_candidate` (around line 600) and `has_implementation_budget` (around line 755); attempt limits are listed by name in `max_implementation_attempts` (around line 728), `has_implementation_budget` (around line 762) and at the top of `_implement_with_routing` (around lines 1018-1036). The constructor (around line 148) keeps its current parameters: tests and `cli.py` (line 124) build it with one runner per provider.

# Decisions (do not reopen)

1. The constructor builds `self.adapters = AdapterRegistry()` and registers, in this order, agy, copilot, codex, cursor, claude (today's dict order), passing the runners and config. The Cursor adapter gets `model_class_for=self._cursor_model_class`, `on_auto_quota=self._remember_cursor_auto_exhaustion`, `budget=self._cursor_implement_budget`; the agy adapter gets `exhausted_pools_for=self._agy_exhausted_pools`.
2. Every `enabled` map becomes `{a.name: a.enabled() for a in self.adapters}`; every attempt limit becomes `adapter.implementation_budget()`; `max_implementation_attempts` is the sum over enabled adapters plus the always-counted Codex and Cursor budgets exactly as today (Codex and Cursor are counted even when disabled today; keep that and add a comment).
3. The five `_<provider>_enabled_for` methods become one `_enabled_for(task, provider, phase=None)` (adapter enabled, and neither the task disable key nor the phase disable key set). Keep `_disable_key` (Codex's legacy key name `provider.codex.disabled`) working.
4. Implementation, review, plan-change and brief-check dispatch do not change in this task (S16-T9 to T11 do that).

# Acceptance criteria

1. New `tests/harness/test_provider_registry.py`: the registry holds the five names in order; disabling Copilot in config removes it from `_choose`'s enabled map (spy on `choose_provider`).
2. A fake sixth adapter registered on the registry in a test is offered to `choose_provider` with no other code change (shows the loops are generic).
3. Existing provider tests pass unchanged, or are updated only where they patched a removed `_<provider>_enabled_for` method.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
