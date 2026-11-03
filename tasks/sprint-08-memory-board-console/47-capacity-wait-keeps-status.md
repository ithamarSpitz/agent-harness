---
id: S8-T47
stage: 3
model_class: worker
timeout_minutes: 25
review: true
allow_protected: false
depends_on:
  - S8-T14
allowed_paths:
  - harness/capacity.py
  - tests/harness/test_capacity_wait_status.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

A capacity pause that is not caused by a provider (for example S8-T14's "computer out of resources" requeue) is not relabeled as a provider error while the harness waits.

# Background

Found by the S8-T14 reviewer (Nov 2026). S8-T14 requeues a task as `PENDING` when a provider CLI dies at start with a Windows resource exit code, and the scheduler records the pause with `run_status = PENDING` and an "out of resources" message. Then `harness run` enters the capacity wait, and `CapacityManager.wait_once` (`harness/capacity.py`) sets the sprint `run_status` to the first task's `WAITING_*` status, or to `WAITING_PROVIDER_ERROR` when no task is waiting. With only `PENDING` tasks, a machine resource pause is shown as a provider error.

# Decisions (do not reopen)

1. `wait_once` sets `run_status` from waiting tasks as today. When no task has a `WAITING_*` status, it keeps the `run_status` already recorded (do not overwrite it); only if none is recorded does it use `WAITING_PROVIDER_ERROR` as today.
2. Waiting time and keep-awake behavior are unchanged.

# Acceptance criteria

1. Sprint `run_status` is `PENDING` (recorded by the scheduler) and all tasks are `PENDING`: after `wait_once` it is still `PENDING`.
2. One task `WAITING_NETWORK`: `run_status` becomes `WAITING_NETWORK` as today.
3. Nothing recorded and no waiting task: `WAITING_PROVIDER_ERROR` as today.
