---
id: S8-T23
stage: 2
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S8-T21
allowed_paths:
  - harness/git_worktree.py
  - harness/cli.py
  - tests/harness/test_worktree_remove.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Report worktree folders that could not be removed -->
# Goal

Removing a task worktree never fails silently; `reset-task` tells the truth.

# Background (found on BookNook sprint S3-polish, Nov 2026)

`remove()` runs `git worktree remove --force` and then `shutil.rmtree(worktree, ignore_errors=True)`. On Windows a briefly held file handle (here a preview `http.server` serving the worktree) made the delete fail silently, and `reset-task` still printed "task-owned worktree/branch cleaned".

# Decisions (do not reopen)

1. `remove()` retries the folder delete up to 5 times with a short pause (Windows file handles), then returns or raises a result naming the folder that remains. Same for the other `ignore_errors=True` delete in `git_worktree.py`.
2. `reset-task` prints `cleaned` only when the folder is really gone; otherwise `could not remove <path>: close programs using it and run reset-task again` and exits non-zero.
3. Other callers log a warning with the path and continue (S8-T21 makes a leftover folder safe).

# Acceptance criteria

1. A fake delete that fails twice then succeeds removes the folder and reports success.
2. A delete that always fails makes `reset-task` print the path and exit non-zero.
3. Existing worktree tests pass.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
