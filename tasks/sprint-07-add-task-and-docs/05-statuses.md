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
4. **A stalled or timed-out reviewer is not a failed attempt.** Today, when the reviewer call stalls (`stall_timeout_minutes`) or times out, the task spends an implementation attempt and the coding agent reruns with feedback `Agent stalled`. Instead: set `WAITING_TIMEOUT`, keep the worktree and the implementation as they are, spend no attempt, and on resume continue with that phase (resuming the review without rerunning verification is S7-T7). The same applies to a stalled implementation call: `WAITING_TIMEOUT`, no attempt spent, rerun that phase. After 3 consecutive stalls of the same phase for the same task, fall back to the next provider for that phase the way quota fallback does; if none is left, BLOCKED with the reason `stalled 3 times`.
5. **MERGING** is set on every task of a stage when `_integrate_stage` starts, and becomes `DONE` (pass) or goes back as today (rollback) when it ends. Making that transition safe when worktree cleanup fails is S7-T8.
6. **Compatibility.** A `state.json` written with old names loads: map `RUNNING`→`IMPLEMENTING`, `VERIFYING`→`CHECKING`, `VERIFIED`→`PASSED`, `WAITING_FOR_QUOTA`→`WAITING_QUOTA`, `WAITING_FOR_CAPACITY`→`WAITING_PROVIDER_ERROR`.
7. Messages name the real provider and cause: today a Codex network failure prints "Waiting for Cursor provider capacity" (`scheduler.py`, `run_sprint`) and the quota message always says "Cursor". Print the provider recorded for that task's call and the specific cause, e.g. `Waiting (network, codex): S6-T1, S6-T2`.
8. Every place that prints or compares statuses (`status`, `failure`, `quota status`, `usage`, recovery commands, messages) uses the new names.

# Split into follow-up tasks (not in this task's scope)

These findings from this task's review are real and each has its own task in the next stage. Do not fix them here, and the reviewer must not fail this task for them:
- Cursor answers with `is_error: true` and exit code 0 → S7-T6.
- Resuming a review after a timeout must not rerun verification → S7-T7.
- `MERGING` → `DONE` must stay consistent if worktree removal fails → S7-T8.

# Acceptance criteria

1. Fake provider outputs for each cause produce the matching waiting status, and each resumes like capacity waits do today without spending an attempt.
2. A stage being integrated shows `MERGING` for its tasks; after a successful fake integration they are `DONE`.
3. An old `state.json` with every old name loads with the mapped names.
4. Old `capacity_patterns` / `transient_patterns` config keys still work.
5. A fake reviewer that stalls leaves the task in `WAITING_TIMEOUT` with the same attempt count and an untouched worktree; resume continues the review phase; three stalls in a row switch provider.
6. No old status name appears in `harness/` except in the compatibility mapping.
