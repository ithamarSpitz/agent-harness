---
id: S18-T6
stage: 2
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S18-T5
allowed_paths:
  - harness/provider_runner.py
  - harness/config.py
  - tests/harness/test_escalation.py
  - tests/harness/test_retry_budget.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- After 2 failed tries the next provider by quota takes over at its strongest level -->
# Goal

A task that failed twice on one provider moves to the next provider chosen by quota, which starts at its strongest level, instead of spending that provider's whole ladder first (user answer R03).

# Background

Sprint S18 (harness 0.27.0 -> 0.28) fixes four field bugs and changes routing to the user's rules:
- Kiro smoke test (2026-11-20): Kiro answers `**STATUS**: COMPLETED`; `kiro_runner._has_report` only accepts a plain `STATUS:` line, so every Kiro implementation counts as a missing report.
- The learned memory estimate per agent (`resources.memory_per_agent_gb`) only goes up (6.3 GB after one big peak), so only one agent runs at a time.
- BookNook S24-T2 (2026-11-20): the verification command `node_modules/.bin/deno check scripts/refresh-all.ts` fails on Windows because the harness runs commands through `cmd.exe`, which cannot start a program path written with `/`. Codex and Claude each spent their attempts on the same failure; then the task showed `WAITING_QUOTA ... none usage quota exhausted during implement`, a wrong label: no quota was out, the attempt budgets were.
- Routing (user rules, design answers Q05, R02, R03): among providers that can take the call, the one whose quota resets soonest goes first; after 2 failed tries the next provider by quota takes over at a smarter level.

`harness/provider_runner.py` `_implement_with_routing` (around line 955) excludes a provider only when `_provider_attempt(task, name) >= adapter.implementation_budget()` (its ladder length, usually 3), and passes `provider_attempt=self._provider_attempt(task, provider)` in `CallContext`; adapters pick their ladder step from that number (for example `codex.implementation_profile(ctx.provider_attempt)` in `harness/adapters/codex.py`). `_consume_provider_attempt` counts consumed attempts per provider. Config lives in `harness/config.py` (`RetryConfig`, `retry:` section).

# Decisions (do not reopen)

1. New setting `retry.escalate_after_failures: int = 2` (validation: >= 1).
2. In `_implement_with_routing`, a provider with `escalate_after_failures` or more consumed attempts on this task is also excluded, as long as at least one other enabled provider that is not disabled for the task has fewer consumed attempts than that number and fewer than its budget. When every such provider is at the limit, the old rule (budget only) applies again, so no attempt the task had before is lost.
3. Escalated call: when the task already has at least `escalate_after_failures` consumed attempts in total and the chosen provider has fewer than its budget, the call's `CallContext.provider_attempt` is `adapter.implementation_budget() - 1` (its last, strongest step). The consumed-attempt count still goes up by one as today.
4. The routing log line for an escalated call says `escalated after <n> failed tries; last on <previous provider>`, where `<n>` is the task's total consumed attempts and `<previous provider>` is the task meta `<sprint>.<task>.provider.implementer` (the provider of the last implementation attempt).
5. Reviews, plan repair and brief-check are unchanged.

# Acceptance criteria

1. `tests/harness/test_escalation.py` with fake adapters (budgets 3): two failures on Codex, then the third attempt goes to Claude with `provider_attempt == 2`; with Claude disabled for the task, the third attempt stays on Codex at its step 2 (old rule); `escalate_after_failures: 3` keeps the old order for three tries.
2. A test in the same file: `escalate_after_failures: 0` is a config validation error.
3. Existing tests in `tests/harness/test_retry_budget.py` pass, updated only where they encoded "the same provider retries three times while another has usage".

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
