---
id: S7-T7
stage: 4
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S7-T5
allowed_paths:
  - harness/scheduler.py
  - tests/harness/test_stall_resume.py
context: []
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

When a review stalled or timed out, resume runs only the review again: no new implementation and no rerun of the task verification commands.

# Background

Today resuming a reviewer timeout goes through `_verify_review_commit` (`harness/scheduler.py`), which reruns task verification before the review. Verification already passed for this exact commit.

# Decisions (do not reopen)

1. Record the commit that passed verification in the task runtime (if not already recorded). On resume of a review wait, if the worktree HEAD equals that commit and the worktree is clean, go straight to review.
2. If HEAD differs or the worktree is dirty, run verification first as today (safety).
3. No attempt is spent either way.

# Acceptance criteria

1. A fake run whose reviewer stalls, then resumes: verification commands run once in total, the agent runs once, the reviewer twice.
2. A changed worktree before resume makes verification run again.
3. Existing scheduler, capacity and quota tests pass.

Rules: change only `allowed_paths`; tests use fakes, never real agents, GitHub or the network; statuses use the S7-T5 names.
