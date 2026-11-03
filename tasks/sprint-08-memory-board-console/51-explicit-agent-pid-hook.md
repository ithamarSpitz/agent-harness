---
id: S8-T51
stage: 9
model_class: hard_worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S8-T8
  - S8-T48
  - S8-T49
allowed_paths:
  - harness/scheduler.py
  - harness/runner.py
  - harness/codex_runner.py
  - harness/provider_runner.py
  - harness/verifier.py
  - tests/harness/test_agent_pid_hook.py
  - tests/harness/test_scheduler_memory.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

The memory sampler learns each agent's process id from the runner that starts it, instead of replacing `subprocess.Popen` for the whole program.

# Background

S8-T8 (Nov 2026) samples the peak memory of each agent call. Because the runners (`harness/runner.py` for Cursor, `harness/codex_runner.py` for Codex) create the agent `Popen` and were outside its allowed paths, it installs a wrapper as `subprocess.Popen` (module-wide) while any agent call runs (`_PopenObserver` / `_observed_popen` in `harness/scheduler.py`) and routes new PIDs to the calling thread. It works today, but replacing a library class with a function for the whole process is fragile: any code that subclasses `subprocess.Popen`, checks `isinstance(x, subprocess.Popen)`, or evaluates `subprocess.Popen[...]` at run time while an agent runs would break, and every short helper process of the thread is sampled too.

# Decisions (do not reopen)

1. The Cursor and Codex runners accept an optional `on_process_start(pid)` callback (passed down through `ProviderAgentRunner` for implementation, retry and review calls) and call it right after creating the agent process.
2. The scheduler passes a callback that starts the peak sampler for that PID. Verification commands report their PID the same way: the `Verifier` methods the scheduler calls take an optional `on_process_start(pid)` argument and pass it down to `_run_timed_command`, which calls it right after its `Popen`. The scheduler passes its per-task callback as an argument; it never replaces or patches methods on the shared `Verifier` (tasks verify concurrently on one `Verifier`).
3. Remove `_PopenObserver`, `_observed_popen` and the global `subprocess.Popen` replacement completely.
4. Learned-estimate behavior from S8-T8 stays the same (max of observed peaks, never lowered automatically, sampler failures never fail a task).

# Acceptance criteria

1. After an agent call, `subprocess.Popen` is the original class at all times (assert identity during the call with a fake runner that checks it).
2. Two concurrent fake agent calls each sample only their own PID.
3. Two tasks verifying at the same time each get only their own verification PIDs, and the shared `Verifier` has no attribute changed afterwards (test with two threads and a barrier).
4. Existing S8-T8 tests pass (update them only where they relied on the global wrapper).
