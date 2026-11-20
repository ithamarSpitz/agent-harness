---
id: S18-T1
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/report_markers.py
  - harness/kiro_runner.py
  - harness/agy_runner.py
  - tests/harness/test_report_markers.py
  - tests/harness/test_kiro_runner.py
  - tests/harness/test_agy_runner.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- One report-line parser that accepts markdown emphasis -->
# Goal

Kiro and agy answers count as reports when the `STATUS:` or `VERDICT:` line is written with markdown emphasis, so a Kiro implementation is no longer a missing report.

# Background

Sprint S18 (harness 0.27.0 -> 0.28) fixes four field bugs and changes routing to the user's rules:
- Kiro smoke test (2026-11-20): Kiro answers `**STATUS**: COMPLETED`; `kiro_runner._has_report` only accepts a plain `STATUS:` line, so every Kiro implementation counts as a missing report.
- The learned memory estimate per agent (`resources.memory_per_agent_gb`) only goes up (6.3 GB after one big peak), so only one agent runs at a time.
- BookNook S24-T2 (2026-11-20): the verification command `node_modules/.bin/deno check scripts/refresh-all.ts` fails on Windows because the harness runs commands through `cmd.exe`, which cannot start a program path written with `/`. Codex and Claude each spent their attempts on the same failure; then the task showed `WAITING_QUOTA ... none usage quota exhausted during implement`, a wrong label: no quota was out, the attempt budgets were.
- Routing (user rules, design answers Q05, R02, R03): among providers that can take the call, the one whose quota resets soonest goes first; after 2 failed tries the next provider by quota takes over at a smarter level.

`harness/kiro_runner.py` `_has_report` (around line 158) needs a line that starts with `STATUS:`; Kiro wrote `**STATUS**: COMPLETED`. `harness/agy_runner.py` `_has_report` (around line 302) strips only one leading `#` or `*`, so `**STATUS:**` also fails there.

# Decisions (do not reopen)

1. New module `harness/report_markers.py` with:
    - `def report_value(text: str, marker: str) -> str | None`: for the first line whose marker matches, return the text after the colon, stripped of emphasis and spaces; `None` if no line matches. `marker` is `"STATUS"` or `"VERDICT"`.
    - `def has_report(text: str, phase: str) -> bool`: `implement` looks for STATUS, `review` for VERDICT, any other phase returns True.
2. A line matches when, after leading spaces, an optional list or heading prefix (`-`, `*`, `>`, one to six `#`) and any run of `*`, `_` or backticks, the marker word follows case-insensitively, then optional emphasis characters, then `:` (the colon may sit inside or outside the emphasis). Accepted examples: `STATUS: done`, `**STATUS**: done`, `**STATUS:** done`, `## STATUS: done`, `- **VERDICT:** PASS`, `__VERDICT__: FAIL`. Not accepted: `The STATUS: done` (text before the marker), `STATUSES: x`.
3. `report_value` strips emphasis from the value too: `**STATUS:** **blocked**` gives `blocked`.
4. Both runners' `_has_report` call `has_report`. Keep the method names (other code and tests use them).

# Acceptance criteria

1. `tests/harness/test_report_markers.py` covers every example in decision 2 and 3, both ways.
2. A test in `tests/harness/test_kiro_runner.py` with a fake stream whose final text is `**STATUS**: COMPLETED` (as Kiro wrote it) is a successful implementation, not a report fault.
3. A test in `tests/harness/test_agy_runner.py`: a response with `**STATUS:** done` is a report.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
