---
id: S9-T25
stage: 6
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S9-T24
allowed_paths:
  - harness/scheduler.py
  - harness/cli.py
  - tests/harness/test_hold_release.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- harness hold / release: pause one task without stopping the run -->

# Goal

The operator can pause one task of a running sprint (to fix its brief) and let it continue later, while the other tasks keep running, without restarting the harness.

# Background

Today the only ways to stop a task are a stop request for the whole sprint (`.harness/stop-request`, every task stops) or killing processes. On Nov 3 2026 the operator had to stop all of S9 to keep one task (S9-T18) from spending an attempt that could not pass.

# Decisions (do not reopen)

1. `harness hold <task>` writes the task id into `.harness/hold-request` (JSON `{"tasks": [...]}`, created if missing, safe for concurrent writers like the stop request). The running scheduler reads it each loop: a held task starts no new attempt or review; a call already running finishes normally, then the task becomes `BLOCKED` with `last_error` `held by operator` (its worktree and attempt count are kept).
2. `harness release <task> [--fresh-budget]` removes it from the hold file and sets the task to `RETRY` (`--fresh-budget` resets attempts and provider budgets like `run-task --fresh-budget`; reuse that code). A running scheduler picks the released task up in the current stage without a restart; with no run going, the next `resume` does.
3. `harness status` shows held tasks as `held`.
4. The stop request and the hold file are both checked before every attempt and every review inside a task's own attempt loop (`_run_task_inner`), not only before a task starts: on Nov 3 2026 a stop request was pending while S9-T18 started its third attempt from inside its loop. A stopped task keeps its state (`RETRY`, attempt count unchanged) so `resume` continues it.
5. Holding a task that is not in the current sprint, or releasing one that is not held, prints a clear message and changes nothing.

# Acceptance criteria

1. Fake scheduler with two running tasks: `hold` on one stops it after its current call; the other finishes and nothing else stops.
2. `release` puts it back and the same running scheduler runs it again; `--fresh-budget` resets its attempts.
3. Status shows `held`; the error cases print messages and change nothing.
4. With a stop request written while a task waits between attempts, no further attempt starts and the task is left in `RETRY` with its attempt count.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
