---
id: S8-T40
stage: 2
model_class: worker
timeout_minutes: 25
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - tests/harness/test_watchdog_usage.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

The watchdog tests in `tests/harness/test_watchdog_usage.py` pass reliably on a busy computer.

# Background

On Nov 2 2026 `test_watchdog_allows_silent_agent_with_repository_activity` failed once (`assert result.ok`, 224 other tests passed) while two task verifications and another project's run were using the machine. The test sets `stall_timeout_minutes: 0.01` (0.6 seconds) and a fake agent that writes every 0.1 seconds, so any pause over 0.6 seconds (Python start-up, disk, CPU load) looks like a stall.

# Decisions (do not reopen)

1. Change only test timings: allow at least 10 times the activity interval as the stall timeout in tests that expect no stall (for example writes every 0.2 seconds with a 5 second stall timeout), and keep the total test time under 10 seconds.
2. Tests that expect a stall keep checking a real stall, with the same generous margin in the other direction.
3. No change to harness code.

# Acceptance criteria

1. The file's tests pass 20 times in a row while another `pytest -q tests/harness` runs in parallel (state the evidence in your report).
2. Every test in the file still checks what it checked before.
