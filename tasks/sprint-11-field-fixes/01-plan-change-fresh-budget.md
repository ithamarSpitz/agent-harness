---
id: S11-T1
stage: 1
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/plan_change.py
  - tests/harness/test_plan_change_reset.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- plan-change apply gives reset tasks a fresh budget (report item 4) -->
# Goal

A task that `harness plan-change apply` resets really runs again, instead of going straight back to BLOCKED.

# Background

`PlanChangeManager.apply` (`harness/plan_change.py`, around line 330) sets `status`, `attempt`, `last_error`, `review_feedback` and `waiting_phase` for each affected task that is not DONE, but keeps `provider_attempts`, `stall_counts`, `started_at`, `finished_at` and the per-task provider disables. Seen on BookNook S6: S6-T8 had `provider_attempts {"cursor": 3}`, was reset by CR-0001, and on resume went straight back to BLOCKED without starting an agent. `harness reset-task` already does this right (`harness/cli.py` `cmd_reset_task`: `_reset_implementation_budget` and `clear_task_quota_disables`).

# Decisions (do not reopen)

1. For each affected task that is not DONE, `apply` resets exactly what `reset-task` resets for the budget: `attempt`, `provider_attempts`, `stall_counts`, `started_at`, `finished_at`, `resource_launch_failures`, and clears the task's provider disables. Reuse existing helpers by importing them; do not copy their code. If a helper lives only in `harness/cli.py` and importing it would be circular, call the state-level `clear_task_quota_disables` and reset the fields inline.
2. DONE tasks keep today's behavior (they go to `plan.pending_revalidation`).

# Acceptance criteria

1. New test `tests/harness/test_plan_change_reset.py`: a task with `provider_attempts {"cursor": 3}`, `attempt 3`, status BLOCKED and a `provider.cursor.disabled` meta flag, after a real `propose` + `apply` that lists it as affected, has empty `provider_attempts`, `attempt 0`, no disable flag, status PENDING; and a scheduler run with a fake runner starts an implementation call for it.

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
