---
id: S8-T22
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/provider_runner.py
  - tests/harness/test_worktree_guard.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Check the worktree before every agent call -->
# Goal

A last safety check: no agent call (implementation, retry, review) starts unless its workspace is the top level of its own Git worktree.

# Background (found on BookNook sprint S3-polish, Nov 2026)

A stale plain folder was used as a worktree; Git inside it resolved to the integration checkout and the agent edited the main project. S8-T21 fixes the cause; this guard catches any other way it could happen.

# Decisions (do not reopen)

1. Before each agent call in `harness/provider_runner.py`, run `git -C <workspace> rev-parse --show-toplevel` and compare (resolved, case-insensitive on Windows) with the workspace path.
2. If they differ or the command fails: do not call the agent; the task becomes `BLOCKED` with `workspace <path> is not its own Git worktree (top level is <x>)`, no attempt spent.

# Acceptance criteria

1. A real temporary worktree passes the check.
2. A plain folder inside a repository fails it, the agent fake is never called, and the task is BLOCKED with the message.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
