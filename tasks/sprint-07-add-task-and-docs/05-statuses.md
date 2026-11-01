---
id: S7-T5
stage: 3
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S7-T2
allowed_paths:
  - harness/**
  - tests/harness/**
context: []
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

Task statuses say exactly what is happening, without getting longer. Today `RUNNING`, `VERIFYING` and `VERIFIED` are vague, the stage merge has no status at all, and `WAITING_FOR_CAPACITY` covers four different causes (network failure, provider overload, provider server error, agent call timeout).

# Decisions (do not reopen)

1. **New status names** (`TaskStatus` in `harness/models.py`):

   | Old | New | Meaning |
   |---|---|---|
   | `PENDING` | `PENDING` | not started |
   | `READY` | `READY` | unchanged |
   | `RUNNING` | `IMPLEMENTING` | the coding agent is working |
   | `VERIFYING` | `CHECKING` | task verification commands are running |
   | `REVIEWING` | `REVIEWING` | the reviewer agent is working |
   | `VERIFIED` | `PASSED` | passed checks and review; waiting for the other tasks of its stage |
   | (none) | `MERGING` | its stage is being merged and the stage checks are running |
   | `DONE` | `DONE` | merged and the stage passed |
   | `RETRY` | `RETRY` | unchanged |
   | `BLOCKED` | `BLOCKED` | unchanged |
   | `WAITING_FOR_QUOTA` | `WAITING_QUOTA` | usage limit reached |
   | `WAITING_FOR_CAPACITY` | `WAITING_NETWORK` | could not connect (connection failed / lost / reset, network or transport error, websocket) |
   | | `WAITING_PROVIDER_BUSY` | provider overloaded or out of capacity (the current capacity patterns) |
   | | `WAITING_PROVIDER_ERROR` | provider server error (5xx, service unavailable, internal server error) |
   | | `WAITING_TIMEOUT` | the agent call exceeded its timeout and is treated as transient today |

2. **Classification.** Split today's `capacity` / `transient` classification (Codex in `codex_runner.py`, Cursor in `runner.py`, patterns in `config.py`) into the four causes above. Keep the existing pattern lists' contents; move each existing pattern into the matching new list (network, busy, provider error); timeouts come from the timeout flag, not patterns. Unknown transient errors map to `WAITING_PROVIDER_ERROR`. Old config keys (`capacity_patterns`, `transient_patterns`) still load and are merged into the new lists.
3. **Behavior unchanged.** All four new waiting statuses behave exactly like `WAITING_FOR_CAPACITY` today (same retry interval, no attempt spent, same resume path). `WAITING_QUOTA` behaves like `WAITING_FOR_QUOTA`.
4. **MERGING** is set on every task of a stage when `_integrate_stage` starts, and becomes `DONE` (pass) or goes back as today (rollback) when it ends.
5. **Compatibility.** A `state.json` written with old names loads: map `RUNNING`→`IMPLEMENTING`, `VERIFYING`→`CHECKING`, `VERIFIED`→`PASSED`, `WAITING_FOR_QUOTA`→`WAITING_QUOTA`, `WAITING_FOR_CAPACITY`→`WAITING_PROVIDER_ERROR`.
6. Messages name the real provider and cause: today a Codex network failure prints "Waiting for Cursor provider capacity" (`scheduler.py`, `run_sprint`) and the quota message always says "Cursor". Print the provider recorded for that task's call and the specific cause, e.g. `Waiting (network, codex): S6-T1, S6-T2`.
7. Every place that prints or compares statuses (`status`, `failure`, `quota status`, `usage`, recovery commands, messages) uses the new names.

# Acceptance criteria

1. Fake provider outputs for each cause produce the matching waiting status, and each resumes like capacity waits do today without spending an attempt.
2. A stage being integrated shows `MERGING` for its tasks; after success they are `DONE`.
3. An old `state.json` with every old name loads with the mapped names.
4. Old `capacity_patterns` / `transient_patterns` config keys still work.
5. No old status name appears in `harness/` except in the compatibility mapping.
