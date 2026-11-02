---
id: S8-T30
stage: 3
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on:
  - S8-T14
allowed_paths:
  - harness/codex_runner.py
  - tests/harness/test_codex_watchdog.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Codex stall watchdog counts file activity like Cursor -->
# Goal

A Codex agent that is working (changing files in its worktree) but printing nothing is not killed as stalled.

# Background

Found while writing the spec (S7, Nov 2026): the Cursor watchdog (`harness/runner.py`) treats an agent as alive when either its CLI output or its worktree files changed recently (`last_activity = max(output, repo)`). The Codex watchdog (`harness/codex_runner.py`) only watches stdout/stderr, so a long silent edit can be killed after `stall_timeout_minutes`.

# Decisions (do not reopen)

1. The Codex watchdog uses the same rule as Cursor: last activity is the latest of CLI output and worktree file activity, sampled the same way (reuse the Cursor helper, do not copy it).
2. Stall handling after detection is unchanged.

# Acceptance criteria

1. A fake Codex process that prints nothing but writes a file every few hundred milliseconds for longer than the stall timeout is not stalled (use generous timings so it holds under load).
2. A fake process that is silent and writes nothing is stalled.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
