---
id: S8-T41
stage: 7
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on:
  - S8-T20
  - S8-T21
allowed_paths:
  - harness/scheduler.py
  - tests/harness/test_resume_worktree.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

A task that resumes with a recorded worktree also goes through the registered-worktree check, so it never runs in a stale plain folder.

# Background

Found by the S8-T21 reviewer (Nov 2026). S8-T21 made `WorktreeManager.create()` (`harness/git_worktree.py`) accept an existing path only if `git worktree list --porcelain` lists exactly that path on the task branch, and otherwise replace it with a fresh worktree. But the scheduler's task runner (`harness/scheduler.py`, the block starting `if not runtime.worktree or not Path(runtime.worktree).exists():`) skips `create()` whenever the recorded folder exists, so a resumed task whose registration disappeared still runs in the plain folder. S8-T22's guard catches it before the agent call by blocking the task; this task makes resume repair it instead.

# Decisions (do not reopen)

1. When `runtime.worktree` exists, the scheduler calls `create(task.id, base_ref)` too (it reuses a registered worktree unchanged and replaces an unregistered folder). Keep the existing rule that a reused worktree keeps its recorded `runtime.base_ref`.
2. If `create()` replaced the folder (it was not registered), treat it as a new worktree: record the new branch and path, set `runtime.base_ref` to the current `base_ref`, and log one line `worktree for <task> was not registered; created a fresh one`.
3. If `create()` raises, the task becomes BLOCKED with the error message, no attempt spent.

# Acceptance criteria

1. A task whose recorded worktree is a plain folder (not in `git worktree list`) resumes in a fresh registered worktree, with the log line (fakes for the agent).
2. A task whose recorded worktree is registered resumes in the same folder with its old `base_ref`.
3. Existing scheduler tests pass.
