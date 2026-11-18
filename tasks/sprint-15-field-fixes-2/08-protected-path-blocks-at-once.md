---
id: S15-T8
stage: 2
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/scheduler.py
  - harness/cli.py
  - tests/harness/test_scheduler_integration.py
  - tests/harness/test_add_task.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- A protected-file failure blocks the task at once; add-task warns about it -->
# Goal

A task that changes a protected file it is not allowed to change goes straight to BLOCKED with a clear reason, instead of retrying on every provider; and `harness add-task` warns about such briefs before they run.

# Background

Sprint S15 fixes what went wrong on 2026-11-18 in the harness's own S14 run and in BookNook's S16 run (harness 0.24.0). Evidence: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` items 20-22, and the S14 logs summarized in each brief. agy is turned off in this repo's `harness.yaml` until the agy tasks here are merged; do not turn it on.

BookNook item 22: S16-T4 listed protected `docs/contract.md` in allowed_paths with `allow_protected: false`. The implementer made the right edit, verification failed with `Protected files changed: docs/contract.md` (`harness/verifier.py` around line 252), and the scheduler retried on agy and Copilot until every provider was disabled, then showed `WAITING_QUOTA (none)` instead of BLOCKED. The scheduler handles a failed verification around `harness/scheduler.py` line 1320 (`if not verification.ok:` sets RETRY). `harness validate <SPRINT>` already warns through `lint_brief` (`harness/brief_lint.py`, code `protected`); `cmd_add_task` (`harness/cli.py` around line 266) does not.

# Decisions (do not reopen)

1. In the scheduler, when a failed verification's failures include a `Protected files changed:` entry and `task.allow_protected` is false, block the task immediately with `_block_task` and the message `Protected files changed: <files>. The brief must set allow_protected: true (or the change belongs in another task).`
2. Other verification failures keep retrying as today.
3. `cmd_add_task` runs `lint_brief` on the queued brief and prints each finding as `WARN  brief <code>: <message>` (errors as `ERROR`); an error finding refuses to queue the task with exit code 1.

# Acceptance criteria

1. `tests/harness/test_scheduler_integration.py`: a fake implementation that changes a protected file with `allow_protected: false` ends BLOCKED after one attempt, with the message above; no second provider is called.
2. The same change with `allow_protected: true` passes verification.
3. `tests/harness/test_add_task.py`: adding a brief that lists a protected path with `allow_protected: false` prints an `ERROR brief protected` line and exits 1; a clean brief queues as before.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
