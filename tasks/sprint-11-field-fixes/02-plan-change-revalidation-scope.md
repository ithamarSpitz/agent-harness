---
id: S11-T2
stage: 2
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S11-T1
allowed_paths:
  - harness/plan_change.py
  - tests/harness/test_plan_change_reset.py
  - tests/harness/test_plan_change_revalidation.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- plan-change revalidation only picks tasks that really changed (report item 5) -->
# Goal

After `plan-change apply`, only tasks whose brief or roadmap entry actually changed between `propose` and `apply` are put into `plan.pending_revalidation`.

# Background

BookNook S6, 2026-11-02: an apply that changed only S6 tasks put S0-T1, S1-T1..T10 and S2-T1 into `plan.pending_revalidation`; their old verification commands (Docker for `test:db`, two stale checks) then blocked the resume. The comparison uses `_task_signature` (`harness/plan_change.py` line 35), taken at `propose` time (line 99) and at `apply` time (line 296).

# Decisions (do not reopen)

1. First reproduce it in a test: a project with several sprints where old DONE tasks exist; propose a change touching one task of the newest sprint; apply. Find what makes the signature of untouched tasks differ (likely candidates: path normalization of brief paths, root-relative versus absolute paths, line endings, fields read from runtime state, or a baseline missing tasks that were not loaded at propose time). Fix that cause.
2. A task whose signature is missing from the propose-time baseline is NOT treated as changed (log one line naming it instead).
3. Report the root cause in one paragraph in your final message.

# Acceptance criteria

1. New test `tests/harness/test_plan_change_revalidation.py` reproduces the BookNook shape (DONE tasks in 3 older sprints, a change in the newest) and shows only the changed task in `plan.pending_revalidation`; it fails on the old code.
2. A task whose brief text really changed is still revalidated.

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
