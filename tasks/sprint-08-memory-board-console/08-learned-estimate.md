---
id: S8-T8
stage: 4
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S8-T2
  - S8-T6
allowed_paths:
  - harness/scheduler.py
  - tests/harness/test_scheduler_memory.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Learn memory per agent -->
# Goal

The scheduler learns how much memory one agent of this project really uses and remembers it.

# Decisions (do not reopen)

1. For each agent call (implementation, verification, review) run a `PeakSampler` (S8-T2) on the agent process, sampling every `execution.watchdog_poll_seconds`.
2. After each task, set the estimate to max(stored estimate, observed peak) and save it in `state.json` meta `resources.memory_per_agent_gb`. Never lower it automatically. `current_memory_estimate()` returns the stored value, else the config value.
3. The sampler is injectable; sampling failure never fails a task.

# Acceptance criteria

1. A fake peak higher than the estimate raises it, persists in `state.json`, and the next run starts from it.
2. A lower peak does not lower it.
3. A sampler that raises leaves the task outcome unchanged.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
