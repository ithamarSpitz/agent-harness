---
id: S8-T48
stage: 8
model_class: worker
timeout_minutes: 20
review: true
allow_protected: false
depends_on:
  - S8-T41
allowed_paths:
  - harness/scheduler.py
  - tests/harness/test_memory_wait_message.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

The "waiting for memory" line is shown when the reason changes, not on every poll.

# Background

Found by the operator after S8-T6's review (Nov 2026). In `harness/scheduler.py`, `try_start_one` prints `Waiting (memory): <reason>` and logs it each time admission refuses a start, which is every `execution.watchdog_poll_seconds` (10 s by default) while a task waits. A wait of 20 minutes prints the same line about 120 times.

# Decisions (do not reopen)

1. Print and log the admission reason only when it differs from the last one printed in this run (exact text comparison), and again after a task was started (so a new wait is reported).
2. Nothing else about admission changes.

# Acceptance criteria

1. Fake admission refusing with the same reason over 5 polls prints the line once.
2. A changed reason prints again; after a successful start, the next refusal prints again.
3. Existing scheduler memory tests pass.
