---
id: S18-T5
stage: 1
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/routing.py
  - tests/harness/test_routing.py
  - tests/harness/test_routing_scenarios.py
  - tests/harness/test_provider_choice.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Routing: the provider whose quota resets soonest goes first -->
# Goal

Among providers that can take a call and have no quota about to go to waste, the one whose quota resets soonest is chosen, so the most tokens get used before they reset (user answers Q05 and R02).

# Background

Sprint S18 (harness 0.27.0 -> 0.28) fixes four field bugs and changes routing to the user's rules:
- Kiro smoke test (2026-11-20): Kiro answers `**STATUS**: COMPLETED`; `kiro_runner._has_report` only accepts a plain `STATUS:` line, so every Kiro implementation counts as a missing report.
- The learned memory estimate per agent (`resources.memory_per_agent_gb`) only goes up (6.3 GB after one big peak), so only one agent runs at a time.
- BookNook S24-T2 (2026-11-20): the verification command `node_modules/.bin/deno check scripts/refresh-all.ts` fails on Windows because the harness runs commands through `cmd.exe`, which cannot start a program path written with `/`. Codex and Claude each spent their attempts on the same failure; then the task showed `WAITING_QUOTA ... none usage quota exhausted during implement`, a wrong label: no quota was out, the attempt budgets were.
- Routing (user rules, design answers Q05, R02, R03): among providers that can take the call, the one whose quota resets soonest goes first; after 2 failed tries the next provider by quota takes over at a smarter level.

`harness/routing.py` `choose_provider` (around line 357) builds `has_usage`, puts "urgent" providers (quota about to go to waste, `_waste`) first by waste, then keeps `priority` order for the rest (`final = urgent + [...]`, around line 453). Usage windows carry `reset_at` (`_Usage.windows`). `tests/harness/test_routing_scenarios.py` holds the user's five S9-T32 scenarios.

# Decisions (do not reopen)

1. Keep everything before the final ordering: the Cursor Auto rule, the Claude caps, the review rule (another provider reviews when one has usage; agy never reviews its own work), and urgent providers first by waste.
2. The non-urgent rest is sorted by the provider's soonest reset: the earliest `reset_at` among its windows that are below 100% used. A provider with no known reset (unknown usage or no reset times) sorts after all providers with one. Ties, and the unknown group, keep `priority` order.
3. The reason names it: `soonest reset <local HH:MM>` (date as `YYYY-MM-DD HH:MM` when not today, like `_until_label`) instead of `order` when the chosen provider won by reset time; `order` stays when no provider in the rest had a known reset.
4. `choose_provider` stays pure (no I/O, no state).
5. Scenario tests in `test_routing_scenarios.py` whose expected provider changes only because of this rule are updated, with a comment `# S18: soonest reset first`; any other change in expected result is a regression, so stop and report it.

# Acceptance criteria

1. Tests in `tests/harness/test_routing.py`: Codex resetting in 2 h and Claude in 30 h, both with usage, priority `[claude, codex]`: Codex is chosen with a `soonest reset` reason; an urgent provider still beats a sooner reset; equal resets follow priority; a provider without reset data goes after one with it.
2. A review test: the implementer is skipped even when its reset is soonest, if another provider has usage.
3. Existing tests pass, changed only as decision 5 allows.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
