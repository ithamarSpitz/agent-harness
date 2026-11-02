---
id: S8-T6
stage: 3
model_class: hard_worker
timeout_minutes: 55
review: true
allow_protected: false
depends_on:
  - S8-T1
  - S8-T14
  - S8-T34
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

<!-- Start agents by free memory -->
# Goal

The scheduler starts another agent only when memory allows, and a finished task frees its slot at once.

# Decisions (do not reopen)

1. Today `run_sprint` runs a stage's tasks on a pool sized `max_parallel_agents`. Keep the stage semantics (all tasks of a stage finish before `_integrate_stage`) and the S7 queue pickup, but start tasks one by one under this rule:
   - start a pending task only when `harness.admission.can_start(...)` (S8-T34) allows it, with this harness's project, `free_memory_gb()`, the estimate, `min_free_memory_gb`, `max_parallel_agents`, and `harness.agent_ledger.snapshot()` (S8-T33); it already covers the per-project limit, the always-one rule, memory reserved by agents that just started, and fairness between projects;
   - register each started agent in the ledger (`register_agent`) and unregister it when its call ends (also on errors, in a `finally`);
   - while this harness has ready tasks it cannot start, keep its waiting record up to date with `set_waiting` (0 when nothing is waiting or the run ends);
   - log and show the admission `reason` when a task waits for memory;
   - re-check every `execution.watchdog_poll_seconds` and whenever a task finishes;
   - `estimate` = `memory_per_agent_gb` from config for now (S8-T8 replaces it with the learned value through one method, `current_memory_estimate()`).
2. If `free_memory_gb()` returns `None`, use only `max_parallel_agents` and log once per run.
3. Never stop a running agent because memory dropped.
4. The memory reader is injected into the scheduler (default `harness.memory.free_memory_gb`).

# Acceptance criteria

Fake memory reader and fake agents:
1. Plenty of memory: up to `max_parallel_agents` run at once. Memory for one: tasks run one by one. Almost none: one still runs.
2. When one task finishes, the next pending task starts before the others finish.
3. `None` from the reader falls back to `max_parallel_agents`.
4. All existing scheduler, stage-barrier, quota/capacity and add-task tests pass unchanged.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
