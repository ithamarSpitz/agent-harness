---
id: S8-T38
stage: 3
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/verifier.py
  - harness/config.py
  - tests/harness/test_verification_timeout.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

A verification command that hangs is stopped after a time limit, with a clear failure, instead of holding the task (and the whole stage) forever.

# Background

On Nov 2 2026 S8-T21's `python -m pytest -q tests/harness` (with pytest-xdist workers) hung at about 96% for two hours until the operator killed it. In `harness/verifier.py` the task, global and stage verification commands run with `subprocess.run(..., shell=True)` and no `timeout` (only one code path has one).

# Decisions (do not reopen)

1. New config key `execution.verification_timeout_minutes: float = 30` (`harness/config.py`, positive values only).
2. Every verification command (task, global, stage) gets that timeout. On timeout, kill the whole process tree (Windows: `taskkill /T /F /PID <pid>`; POSIX: start the command in its own process group and kill the group), so pytest-xdist workers do not survive.
3. A timed-out command fails verification with `timed out after N minutes: <command>` plus the last 40 lines of its output so far, so the next attempt can see where it hung. Use `subprocess.Popen` with output collected in a background reader so the partial output is available.
4. The existing timeout path keeps working; reuse one helper for all paths.

# Acceptance criteria

1. A command that sleeps longer than a tiny test timeout fails with the message and its partial output, and leaves no child process running (test with a command that starts a child that also sleeps).
2. Normal commands behave as today (existing tests pass).
