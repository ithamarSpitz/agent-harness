---
id: S18-T3
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/scheduler.py
  - harness/board.py
  - harness/board_loop.py
  - tests/harness/test_scheduler_memory.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- The per-agent memory estimate follows recent peaks, up and down -->
# Goal

One large peak no longer limits a project to one agent forever: the estimate for a new agent is the largest of the recent peaks.

# Background

Sprint S18 (harness 0.27.0 -> 0.28) fixes four field bugs and changes routing to the user's rules:
- Kiro smoke test (2026-11-20): Kiro answers `**STATUS**: COMPLETED`; `kiro_runner._has_report` only accepts a plain `STATUS:` line, so every Kiro implementation counts as a missing report.
- The learned memory estimate per agent (`resources.memory_per_agent_gb`) only goes up (6.3 GB after one big peak), so only one agent runs at a time.
- BookNook S24-T2 (2026-11-20): the verification command `node_modules/.bin/deno check scripts/refresh-all.ts` fails on Windows because the harness runs commands through `cmd.exe`, which cannot start a program path written with `/`. Codex and Claude each spent their attempts on the same failure; then the task showed `WAITING_QUOTA ... none usage quota exhausted during implement`, a wrong label: no quota was out, the attempt budgets were.
- Routing (user rules, design answers Q05, R02, R03): among providers that can take the call, the one whose quota resets soonest goes first; after 2 failed tries the next provider by quota takes over at a smarter level.

`harness/scheduler.py`: `current_memory_estimate` (around line 306) returns the stored `resources.memory_per_agent_gb`; `_remember_memory_peak` (around line 349) only replaces it with a larger value. `harness/board.py` (around line 468) and `harness/board_loop.py` (around lines 102 and 469) read the same meta key to show the estimate.

# Decisions (do not reopen)

1. `_remember_memory_peak` appends the call's peak (GB, rounded to 0.01) to a meta list `resources.memory_peaks_gb`, keeping the last 10, and sets `resources.memory_per_agent_gb` to the largest value in that list. With fewer than 3 peaks stored, the estimate is the largest of those peaks and the configured `execution.memory_per_agent_gb`.
2. An old project with only `resources.memory_per_agent_gb` set and no list: the first new peak starts the list (the old value is not copied in), so the estimate can come down after a few calls.
3. Keep the lock (`self._memory_estimate_lock`) around the read-modify-write. Board and board loop keep reading `resources.memory_per_agent_gb`; change them only if a test shows they must.

# Acceptance criteria

1. Tests in `tests/harness/test_scheduler_memory.py`: peaks 6.3 then 2.0 x 10 give an estimate of 2.0 after the tenth small peak; peaks 2.0, 2.5 with config 1.5 give 2.5; with config 3.0 they give 3.0; the list never holds more than 10 values.
2. A test: a state with only the old key 6.3 and then 10 peaks of 2.0 ends at 2.0.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
