---
id: S8-T18
stage: 2
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S8-T16
allowed_paths:
  - harness/quota.py
  - tests/harness/test_quota_wait.py
  - tests/harness/test_quota_plan_repair.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Quota waits end at the real reset time -->
# Goal

When every provider is out of quota, the harness waits until the earliest known reset, not a fixed 60 minutes.

# Background

`QuotaManager.next_wait_seconds` (`harness/quota.py`) returns the configured interval (60 minutes by default). `harness limits` (`harness/limits.py`) already knows the reset times: Codex 5-hour and weekly windows from the live app-server call, and Cursor's billing cycle end.

# Decisions (do not reopen)

1. `next_wait_seconds()` asks the limits module for the reset times of providers that are currently out (Codex: the window at 100 %; Cursor: billing cycle end when its pool is used up) and returns the time until the earliest one plus 60 seconds.
2. If no reset time is known (call failed, provider not reporting), use the configured interval as today.
3. Never wait less than 60 seconds. When the reset is known, sleep at most 24 hours at a time and re-check after waking.
4. The wait message names the provider and the reset time, for example `Waiting for codex quota until 17:14 (in 3h 21m)`.
5. Limits calls are injectable; tests never call real providers. Existing tests that build a `QuotaManager` (for example `tests/harness/test_quota_plan_repair.py`) must get a fake limits source so they never reach Codex or Cursor.

# Acceptance criteria

1. Fake limits with Codex at 100 % and reset in 3 h: wait is 3 h + 60 s and the message names codex and the time.
2. Both providers out: the earliest reset wins.
3. Limits failure: configured interval.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
