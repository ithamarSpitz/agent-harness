---
id: S8-T9
stage: 3
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S8-T7
allowed_paths:
  - harness/console_commands.py
  - tests/harness/test_console_commands.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Console: commands that change state -->
# Goal

Console commands that act: add a task, retry a blocked task, stop the run.

# Decisions (do not reopen)

1. Each asks `y/N` through the context's confirm callback before acting and calls the same function as its CLI command:
   - `add <brief path> [--stage auto|current|next]`: same as `harness add-task`;
   - `retry <id>`: for a BLOCKED task, same as `harness rerun-blocker <sprint> --task <id>`; for any other status, one line explaining why not;
   - `stop`: writes the stop request file `.harness/stop-request` (sprint id and time). S8-T10 makes the scheduler honor it.
2. `quit`: returns a quit signal to the caller (no state change, no confirmation).

# Acceptance criteria

1. Without confirmation nothing changes; with it each command calls the CLI function with the right arguments (fakes record calls).
2. `retry` on a non-blocked task changes nothing and says why.
3. `stop` writes the request file with the sprint id.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
