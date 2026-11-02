---
id: S8-T21
stage: 1
model_class: hard_worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/git_worktree.py
  - tests/harness/test_worktree_stale.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Never use an unregistered folder as a task worktree -->
# Goal

An agent never runs in a folder that is not a registered Git worktree of its task.

# Background (found on BookNook sprint S3-polish, Nov 2026)

`reset-task` printed "cleaned" but `.harness/worktrees/S3-T1` stayed on disk as a plain folder (no longer in `git worktree list`). The next `run-task` reused it, because `WorktreeManager.create()` (`harness/git_worktree.py`) does `if path.exists(): return branch, path`. Inside that plain folder Git resolved to the integration checkout, so the agent edited the main checkout (uncommitted), `npm ci` ran in the main checkout, scope checks saw nothing, and the task blocked with the integration checkout dirty.

# Decisions (do not reopen)

1. `create()`: when the path exists, accept it only if `git worktree list --porcelain` lists exactly that path on the expected branch (`agent/<task>` as today). Otherwise remove the folder and create a fresh worktree with `git worktree add`.
2. If the folder cannot be removed, raise a clear error naming the path and saying to close programs using it; never return it.
3. Existing registered worktrees (normal resume) behave as today.

# Out of scope (another task)

The scheduler's resume path (`harness/scheduler.py`, which reuses `runtime.worktree` without calling `create()` when the folder exists) is fixed by S8-T41. Until then S8-T22's guard stops any agent call in a folder that is not its own worktree. Do not edit the scheduler here, and reviewers should not fail this task for it.

# Acceptance criteria

1. A plain folder (only `graphify-out/` inside) at the worktree path is replaced by a real worktree; the returned path is listed by `git worktree list`.
2. A registered worktree on the right branch is reused unchanged.
3. A registered worktree on another branch, or a folder that cannot be removed (fake), raises the clear error.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
