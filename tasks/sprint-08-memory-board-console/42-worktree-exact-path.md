---
id: S8-T42
stage: 3
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S8-T23
allowed_paths:
  - harness/git_worktree.py
  - tests/harness/test_worktree_exact_path.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

A task worktree path is accepted only when `git worktree list --porcelain` lists exactly that path, not a different folder that a link at that path points to.

# Background

Found by the first S8-T21 reviewer (Nov 2026) and not fixed in S8-T21: `WorktreeManager._registered_branch()` (`harness/git_worktree.py`) calls `.resolve()` on both the expected path and every listed worktree before comparing. A symlink or Windows junction at `.harness/worktrees/<task>` that points to another registered worktree therefore counts as registered, and the agent would run in that other worktree.

# Decisions (do not reopen)

1. Compare paths without following links: normalize both sides with `os.path.abspath` and `os.path.normcase` (case-insensitive on Windows), and treat forward and back slashes alike. Do not call `resolve()` for this comparison.
2. If the expected path itself is a symlink or junction (`os.path.islink`, or a junction on Windows), it is never accepted: `create()` removes the link (not its target) and creates a fresh worktree, as it does for a plain folder.
3. Other uses of `resolve()` in the file stay as they are unless they decide whether a folder is a task worktree.

# Acceptance criteria

1. A link at the task path pointing to another registered worktree is replaced by a fresh worktree, and the other worktree's files are untouched (skip the test with a clear reason when the OS does not allow creating links).
2. A registered worktree reached by its exact path is reused, also when the case of the drive letter differs on Windows.
3. Existing worktree tests pass.
