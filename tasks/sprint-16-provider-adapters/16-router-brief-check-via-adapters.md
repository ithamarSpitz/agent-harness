---
id: S16-T16
stage: 7
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on:
  - S16-T11
allowed_paths:
  - harness/provider_runner.py
  - harness/adapters/codex.py
  - tests/harness/test_adapter_codex.py
  - tests/harness/test_copilot_wiring.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- brief-check goes through the adapter -->
# Goal

`ProviderAgentRunner.brief_check` calls `adapter.brief_check(...)` instead of naming Copilot and Codex.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

`brief_check` is around lines 1449-1500 in `harness/provider_runner.py`. It tries Copilot then Codex, each after an availability check. The Copilot adapter (S16-T6) has `brief_check`; Codex's runner has one (`codex_runner.py` line 460) but the Codex adapter (S16-T2, `harness/adapters/codex.py`, test `tests/harness/test_adapter_codex.py`) does not yet.

# Decisions (do not reopen)

1. Add `brief_check` to `CodexAdapter`: availability check (same unavailable result as its other calls), then the runner's `brief_check` with today's arguments.
2. `ProviderAgentRunner.brief_check` walks the order it uses today and, for each provider, calls `adapter.brief_check(...)` when the adapter has the method (`getattr`), skipping adapters without it.

# Acceptance criteria

1. `tests/harness/test_adapter_codex.py`: `brief_check` with Codex unavailable returns the unavailable result without calling the runner.
2. `tests/harness/test_copilot_wiring.py`: brief-check with Copilot unavailable falls through to Codex, as before; with both unavailable it returns today's result.
3. `grep -n 'provider == "' harness/provider_runner.py` finds no match inside `brief_check`.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
