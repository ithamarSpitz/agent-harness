---
id: S9-T14
stage: 1
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/verifier.py
  - harness/scheduler.py
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

<!-- Verification process ids are passed as an argument, no method swap -->

# Goal

The scheduler learns the process id of each verification command through an argument, without replacing any method on the shared `Verifier`.

# Background

S8-T51 (Nov 2026) removed the process-wide `subprocess.Popen` replacement, but `harness/verifier.py` was outside its allowed paths. So `_verify_with_process_hook` in `harness/scheduler.py` still installs a reference-counted, thread-local dispatcher in place of `Verifier._run_timed_command` while any task verifies. It is safe today, but it patches a shared object and is hard to follow.

# Decisions (do not reopen)

1. The `Verifier` methods the scheduler calls take an optional `on_process_start(pid)` argument and pass it down to `_run_timed_command`, which calls it right after its `Popen` (a failing callback is logged and ignored).
2. The scheduler passes its per-task callback as that argument. Remove `_verify_with_process_hook`, the dispatcher, its thread-local and reference count, and every `setattr` on the verifier.
3. Learned-estimate behavior is unchanged.

# Acceptance criteria

1. Two tasks verifying at the same time on one `Verifier` each get only their own verification PIDs (two threads and a barrier), and `Verifier` has no instance attribute added or changed afterwards.
2. `grep -n "setattr" harness/scheduler.py` shows no use on the verifier.
3. Existing tests pass.
