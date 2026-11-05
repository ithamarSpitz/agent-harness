---
id: S11-T5
stage: 1
model_class: hard_worker
timeout_minutes: 60
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/scheduler.py
  - tests/harness/test_reviewer_fault.py
  - tests/harness/test_launch_failure.py
  - tests/harness/test_launch_fallback_reason.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- A reviewer that fails without a verdict does not cost an implementation attempt (report item 12, any provider) -->
# Goal

When a review call fails for the reviewer's own reasons and gives no verdict, the harness reviews again on another provider; it does not send the task back to implementation.

# Background

`harness/scheduler.py`, around lines 1290-1340: launch failures and waits are handled, but any other review result that is not PASS goes to RETRY with the reviewer's error as feedback, so the task is implemented again. On BookNook S9-T1 every agy review failed in about 3 s with a configuration error (`invalid model selection`); each one consumed an implementation attempt, and the task went BLOCKED although Claude's implementation passed verification 3 times.

# Decisions (do not reopen)

1. **Reviewer fault** = the review call failed (`ok` false) AND its output has no verdict line (use the existing verdict parsing, `parse_review_outcome`). A review that says `VERDICT: FAIL` is a real FAIL and keeps today's behavior.
2. On a reviewer fault: disable that provider for reviews of this task (meta key `<sprint>.<task>.provider.<name>.review_disabled`, reason = first line of the error), log one line `review fault on <provider>: <reason>; reviewing again elsewhere`, and run the review again in the same worktree without another implementation call. Implementation attempts and `provider_attempts` do not change.
3. At most 2 reviewer faults per task attempt. After that, or when no other provider can review, the task goes BLOCKED with the reviewer errors listed (never RETRY).
4. Provider choice for reviews skips a provider with `review_disabled` for that task. If the code that chooses the review provider is outside `allowed_paths`, pass the excluded provider through the existing review call arguments instead and say so in the report.
5. Waits (quota, network, busy) and launch failures keep their current handling; this applies only to results that reach the verdict check today.

# Acceptance criteria

1. New test `tests/harness/test_reviewer_fault.py`: fake reviewer A returns `ok=False` with output `invalid model selection` and no verdict; reviewer B returns `VERDICT: PASS`; the task passes with exactly one implementation call.
2. Same, but B returns `VERDICT: FAIL` with findings: normal RETRY with B's feedback, one implementation call so far.
3. All reviewers fault: BLOCKED after 2 faults, the error lists both, the attempt count is unchanged.
4. A reviewer returning `ok=False` with `VERDICT: FAIL` is a normal FAIL (no provider disabled).

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
