---
id: S8-T16
stage: 1
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/scheduler.py
  - harness/capacity.py
  - tests/harness/test_capacity_in_stage.py
  - tests/harness/test_quota_plan_repair.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Retry network and provider waits inside the stage -->
# Goal

A task paused by a temporary provider problem (`WAITING_NETWORK`, `WAITING_PROVIDER_BUSY`, `WAITING_PROVIDER_ERROR`, `WAITING_TIMEOUT`) is retried while the rest of its stage keeps running, instead of only after the whole stage has finished.

# Background

Today `Scheduler.run_sprint` waits for every task of the stage; capacity-paused outcomes are only collected at the end, recorded as `CAPACITY_WAIT`, and the CLI loop (`harness/cli.py`, `CAPACITY_WAIT` branch) sleeps `capacity.retry_interval_minutes` and resumes. In S6 (Nov 2026) two tasks paused by a short network outage sat idle for over an hour while the other tasks of their stage ran.

# Decisions (do not reopen)

1. Inside a stage, a capacity-paused task is put back in the stage's pending list with `retry_at` = now + `capacity.retry_interval_minutes`. It starts again when `retry_at` has passed and a slot is free, while other tasks keep running. No attempt is spent (same as today).
2. Only when every unfinished task of the stage is capacity-paused does `run_sprint` return as today (`CAPACITY_WAIT`), so the CLI wait loop still handles the "everything is waiting" case.
3. The task keeps showing its waiting status until it restarts.
4. Quota pauses are unchanged (handled by S8-T18).

# Acceptance criteria

1. Fake run: task A pauses with a network cause while task B runs a long time; A restarts after the retry interval (fake clock) and finishes before B, attempt count unchanged.
2. When all unfinished tasks are paused, `run_sprint` returns with `CAPACITY_WAIT` as today.
3. Existing scheduler, capacity, quota and add-task tests pass.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
