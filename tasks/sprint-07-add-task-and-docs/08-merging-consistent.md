---
id: S7-T8
stage: 5
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S7-T7
allowed_paths:
  - harness/scheduler.py
  - tests/harness/test_stage_merging.py
context: []
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

After a stage integration, every task of the stage ends in a consistent status, even if cleaning up a worktree fails.

# Background

Today successful integration (`harness/scheduler.py`, the loop after the stage barrier passes) marks tasks `DONE` one at a time and removes each worktree in between. If a removal fails, the remaining tasks stay `MERGING` forever.

# Decisions (do not reopen)

1. Once the stage barrier has passed, first mark every task of the stage `DONE` and save state once; only then remove worktrees.
2. A failed worktree removal is logged as a warning with the path and never changes task statuses or fails the sprint; the next `harness cleanup` or run removes leftovers.
3. If the barrier fails, every task goes back exactly as today (rollback).

# Acceptance criteria

1. A fake worktree removal that raises for the first task: all tasks end `DONE`, the warning names the path, the sprint continues.
2. A failed barrier leaves no task in `MERGING`.
3. Existing stage-barrier tests pass.

Rules: change only `allowed_paths`; tests use fakes, never real agents, GitHub or the network; statuses use the S7-T5 names.
