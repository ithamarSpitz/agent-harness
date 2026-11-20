---
id: S18-T2
stage: 2
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S18-T1
allowed_paths:
  - harness/scheduler.py
  - harness/usage.py
  - tests/harness/test_scheduler_report_markers.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Scheduler and usage read reports with the shared parser -->
# Goal

Every place that reads an agent's `STATUS:` or `VERDICT:` line accepts markdown emphasis the same way (S18-T1's `harness/report_markers.py`).

# Background

Sprint S18 (harness 0.27.0 -> 0.28) fixes four field bugs and changes routing to the user's rules:
- Kiro smoke test (2026-11-20): Kiro answers `**STATUS**: COMPLETED`; `kiro_runner._has_report` only accepts a plain `STATUS:` line, so every Kiro implementation counts as a missing report.
- The learned memory estimate per agent (`resources.memory_per_agent_gb`) only goes up (6.3 GB after one big peak), so only one agent runs at a time.
- BookNook S24-T2 (2026-11-20): the verification command `node_modules/.bin/deno check scripts/refresh-all.ts` fails on Windows because the harness runs commands through `cmd.exe`, which cannot start a program path written with `/`. Codex and Claude each spent their attempts on the same failure; then the task showed `WAITING_QUOTA ... none usage quota exhausted during implement`, a wrong label: no quota was out, the attempt budgets were.
- Routing (user rules, design answers Q05, R02, R03): among providers that can take the call, the one whose quota resets soonest goes first; after 2 failed tries the next provider by quota takes over at a smarter level.

Plain-line checks remain in `harness/scheduler.py`: `_blocked_open_issues` (around line 632, regex `^\s*STATUS\s*:?...blocked`) and the review loop's `has_verdict` (around line 1384, `line.strip().startswith("VERDICT:")`); and in `harness/usage.py` `parse_review_outcome` (around line 37: PASS needs the exact line `VERDICT: PASS`, findings start after a line starting `VERDICT:`).

# Decisions (do not reopen)

1. `_blocked_open_issues` treats the task as blocked when `report_value(output, "STATUS")` is `blocked` (case-insensitive). The OPEN_ISSUES text search also accepts an emphasized `OPEN_ISSUES` heading (`**OPEN_ISSUES:**`, `## OPEN_ISSUES`).
2. `has_verdict` uses `report_value(output, "VERDICT") is not None`.
3. `parse_review_outcome`: PASS when the verdict value is `PASS` (case-insensitive, emphasis stripped); the findings count starts after the verdict line found the same way. `IMPACT:` and `UI_CHECK:` end the findings also when emphasized.
4. Plain reports behave exactly as before.

# Acceptance criteria

1. `tests/harness/test_scheduler_report_markers.py`: `**VERDICT:** PASS` gives PASS with 0 findings; `**VERDICT**: FAIL` with two finding lines and `**IMPACT:**` gives FAIL with 2; `**STATUS:** blocked` with `**OPEN_ISSUES:** x` returns `x`; the old plain forms give the same results as before.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
