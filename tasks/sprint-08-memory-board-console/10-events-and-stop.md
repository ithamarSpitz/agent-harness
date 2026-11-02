---
id: S8-T10
stage: 5
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S8-T8
allowed_paths:
  - harness/scheduler.py
  - harness/events.py
  - tests/harness/test_events.py
  - tests/harness/test_scheduler_stop.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Event feed and stop request -->
# Goal

The scheduler writes a short event feed for the board, and honors a stop request.

# Decisions (do not reopen)

1. `harness/events.py`: `append_event(root, sprint_id, text)` writes one JSON line (`time`, `sprint`, `text`) to `.harness/events.jsonl`; `read_events(root, sprint_id, limit)` returns the newest first. The file is trimmed to its last 500 lines when it grows past 1000.
2. The scheduler appends events for: status changes, reviewer verdict with finding count, waits with cause and provider, task added, stage merge start and result, release result.
3. Stop request: before starting any new task the scheduler checks `.harness/stop-request` for its sprint. If present: start nothing new, let running agents finish, save state, delete the file, exit with a clear message and a resumable state (same as a quota pause). An event is written.

# Acceptance criteria

1. Events are written for each listed transition in a fake run, newest first on read, and trimming works.
2. A stop request during a fake run stops new starts, running tasks finish, the file is removed, and `resume` continues from there.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
