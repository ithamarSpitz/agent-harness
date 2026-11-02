---
id: S8-T15
stage: 1
model_class: worker
timeout_minutes: 40
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
  - >-
    python -c "import subprocess,sys;rc=[subprocess.run([sys.executable,'-m','pytest','-q','-n','0','tests/harness/test_watchdog_usage.py']).returncode for _ in range(10)];print('10 runs, exit codes:',rc);sys.exit(1 if any(rc) else 0)"
---

<!-- Timing tests that hold under load -->
# Goal

The watchdog tests pass reliably even when the machine is busy (other agents and parallel test workers running).

# Background

In S7 (Nov 2026) `test_watchdog_allows_silent_agent_with_repository_activity` failed once while two agents and the parallel test suite ran (the whole suite took 125 s instead of about 50 s). It uses `stall_timeout_minutes: 0.01` (0.6 s) with a fake agent that writes a file every few milliseconds, so a short scheduling pause looks like a stall.

# Decisions (do not reopen)

1. Keep every existing assertion. Make the stall-related tests independent of machine speed: tests that expect no stall use a stall timeout far above the fake agent's activity interval (at least 20× the interval, and at least 5 s), and tests that expect a stall use a fake agent that stays silent far longer than the timeout.
2. Total run time of `tests/harness/test_watchdog_usage.py` stays under 20 s on an idle machine.
3. Do not change code under `harness/`.

# Acceptance criteria

1. The file passes 10 times in a row (the last verification command runs it 10 times; the harness runs verification while other tasks work, which is the load case).
2. Every test that existed before still exists and asserts the same outcome.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
