---
id: S8-T29
stage: 3
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S8-T23
allowed_paths:
  - harness/cli.py
  - tests/harness/test_retry_budget.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- retry and unblock really restart a task -->
# Goal

`harness retry <task>` and `harness unblock <task>` restart a task with a real fresh budget, so the task does not block again immediately.

# Background

Found while writing SPEC.md (S7, Nov 2026): `cmd_retry` and `cmd_unblock` (`harness/cli.py`) set the status to `RETRY` and `attempt = 0`, but leave `provider_attempts` and `stall_counts` as they were. Live implementation calls are gated by the per-provider budget, so after one used Cursor attempt with the default config, `retry` then `resume` runs zero attempts and the task is BLOCKED again. `rerun-blocker` and `run-task --fresh-budget` already reset those counters correctly.

# Decisions (do not reopen)

1. `retry` and `unblock` reset the same counters `run-task --fresh-budget` resets (reuse that code, do not copy it), and keep the worktree as today.
2. Their output says what was reset.

# Acceptance criteria

1. A task BLOCKED after using its Cursor budget, then `retry` and a fake `resume`: the agent is called again.
2. The same for `unblock`.
3. Existing CLI tests pass.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
