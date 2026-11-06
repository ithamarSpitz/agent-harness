---
id: S11-T12
stage: 4
model_class: worker
timeout_minutes: 30
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

<!-- plan-change apply clears all wait state -->
# Goal

A task reset by `harness plan-change apply` carries no stale wait state: no waiting provider and no stored routing reset time.

# Background

Found by the S11-T9 docs review on 2026-11-06 (two failed reviews): the docs describe this behavior, the merged code does not do it. A docs-only task cannot fix code, so this task fixes the code and S11-T9 documents it afterwards.

`PlanChangeManager.apply` (`harness/plan_change.py`, around line 357) sets `runtime.waiting_phase = None` but leaves `runtime.waiting_provider` (`harness/models.py`) and the meta key `<sprint>.<task>.routing_quota_reset_at` (written by `harness/provider_runner.py`, around line 542). `harness/board.py` (around line 394) still shows the stale provider.

# Decisions (do not reopen)

1. In the non-DONE branch of `apply`, also set `runtime.waiting_provider = None` and set the meta `f"{task.sprint}.{task_id}.routing_quota_reset_at"` to `None` (use the sprint id the manager already has; do not guess the key format: read it from `provider_runner.py`).
2. Change nothing for DONE tasks.

# Acceptance criteria

1. Extend `tests/harness/test_plan_change_reset.py`: a WAITING_QUOTA task with `waiting_phase`, `waiting_provider` and `routing_quota_reset_at` set is applied as affected; afterwards all three are empty and the status is PENDING.

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
