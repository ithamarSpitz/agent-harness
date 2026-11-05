---
id: S11-T10
stage: 1
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/provider_runner.py
  - tests/harness/test_budget_vs_wait.py
  - tests/harness/test_provider_choice.py
  - tests/harness/test_codex_provider.py
  - tests/harness/test_cursor_auto_when_exhausted.py
  - tests/harness/test_quota_fallback_expiry.py
  - tests/harness/test_worktree_guard.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- No provider available right now means wait, not 'attempt budget exhausted' -->
# Goal

When every provider is temporarily out (quota, cap, busy), tasks wait for the earliest reset. They are BLOCKED for "attempt budget exhausted" only when no provider has implementation attempts left.

# Background

Seen live on this repo, 2026-11-05 13:55: S11 started while Codex was at its 5-hour limit, Claude was over its cap, Cursor was at 100% and agy was disabled. All five stage-1 tasks went BLOCKED in 26 seconds with `Attempt budget exhausted`, at attempt 0, without one agent call.

Cause: the scheduler's attempt loop (`harness/scheduler.py` around line 1467) runs only while `self.runner.has_implementation_budget(task)` is true. `ProviderAgentRunner.has_implementation_budget` (`harness/provider_runner.py` around line 721) returns `self._choose(...).provider is not None`, and `_choose` applies live usage, so "nobody has usage right now" reads as "no budget". `ProviderAgentRunner.implement` already handles that case correctly (around line 993: `provider is None` returns `AgentResult(..., quota_exhausted=True)`, which the scheduler turns into a quota wait) but is never reached.

# Decisions (do not reopen)

1. `has_implementation_budget` answers only "does any provider still have implementation attempts left for this task": a provider counts when it is enabled, its per-task attempts are below its sequence length (the existing `exhausted` computation), and it is not disabled for the task for a lasting reason (launch failure, auth, worktree guard). A provider that is only temporarily unavailable (quota or cap, busy, quota-disabled for the task with a reset) still counts. It must not use live usage or the Claude cap.
2. Do not change `implement`; it keeps choosing with live usage and returning the quota-wait result.
3. Keep the `model_class_override` and non-routing branches as they are.

# Acceptance criteria

1. New test `tests/harness/test_budget_vs_wait.py`, through a real `Scheduler` run with fake providers and injected headroom: Codex 5-hour at 100% (reset in 1 hour), Claude over its cap, Cursor at 100%, agy disabled; the task ends in `WAITING_QUOTA` (not BLOCKED), attempt still 0, and the run reports a wait until the earliest reset.
2. Same setup, but every provider has used all its implementation attempts for the task: BLOCKED with `Attempt budget exhausted`.
3. Codex quota-disabled for the task and Claude with attempts left but over its cap: waiting, not blocked.

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
