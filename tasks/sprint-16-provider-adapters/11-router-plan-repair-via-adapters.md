---
id: S16-T11
stage: 6
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on:
  - S16-T10
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

<!-- Plan repair goes through the adapter -->
# Goal

`_plan_change_with_routing` calls `adapter.plan_change(...)` for the chosen provider, with no branch on the provider name.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

`_plan_change_with_routing` is around lines 1385-1445 in `harness/provider_runner.py`, the same shape as the review chain S16-T10 replaced.

# Decisions (do not reopen)

1. Replace the chain with `self.adapters.get(provider).plan_change(task, workspace, failure_summary, timeout_minutes, model_class=model_class, env=env)`; a missing adapter is a launch failure `no adapter for <provider>`; disable reason as in S16-T9.
2. The old non-routing `plan_change` and `_review_with_providers` paths stay; a comment on each says it is the routing-off path and still names Codex and Cursor on purpose.

# Acceptance criteria

1. `tests/harness/test_provider_registry.py`: a plan repair routed to a fake adapter calls its `plan_change` once.
2. Codex unavailable at plan repair falls back as before.
3. `grep -n 'provider == "' harness/provider_runner.py` finds no match inside `_plan_change_with_routing`.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
