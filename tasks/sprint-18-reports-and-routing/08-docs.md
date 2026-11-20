---
id: S18-T8
stage: 4
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S18-T3
  - S18-T7
allowed_paths:
  - README.md
  - CHANGELOG.md
  - SPEC.md
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Docs: report parsing, memory estimate, Windows commands, soonest reset, escalation, blocks -->
# Goal

README, SPEC.md and CHANGELOG describe what S18 merged.

# Background

Sprint S18 (harness 0.27.0 -> 0.28) fixes four field bugs and changes routing to the user's rules:
- Kiro smoke test (2026-11-20): Kiro answers `**STATUS**: COMPLETED`; `kiro_runner._has_report` only accepts a plain `STATUS:` line, so every Kiro implementation counts as a missing report.
- The learned memory estimate per agent (`resources.memory_per_agent_gb`) only goes up (6.3 GB after one big peak), so only one agent runs at a time.
- BookNook S24-T2 (2026-11-20): the verification command `node_modules/.bin/deno check scripts/refresh-all.ts` fails on Windows because the harness runs commands through `cmd.exe`, which cannot start a program path written with `/`. Codex and Claude each spent their attempts on the same failure; then the task showed `WAITING_QUOTA ... none usage quota exhausted during implement`, a wrong label: no quota was out, the attempt budgets were.
- Routing (user rules, design answers Q05, R02, R03): among providers that can take the call, the one whose quota resets soonest goes first; after 2 failed tries the next provider by quota takes over at a smarter level.

README describes routing and reviewer faults around line 61; SPEC.md holds the routing and failure-classification rules (around line 62).

# Decisions (do not reopen)

1. README and SPEC.md: report lines may use markdown emphasis; the memory estimate is the largest of the last 10 peaks; Windows verification commands with `/` program paths; soonest reset first after urgent providers; escalation after `retry.escalate_after_failures` (default 2) failed tries to the next provider at its strongest step; the same-failure block and the used-up-budget wait or block.
2. CHANGELOG: an "Unreleased" entry with the same points in a few lines.
3. Describe only what the code does; if code and this brief disagree, follow the code and report it. Code gaps are reported, not fixed here.

# Acceptance criteria

1. Every setting, message and behavior named in the new text exists in the code.
2. Nothing outside these three files changes.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
