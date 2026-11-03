---
id: S9-T17
stage: 2
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on:
  - S9-T6
allowed_paths:
  - harness/home.py
  - harness/agent_ledger.py
  - harness/outcomes.py
  - tests/conftest.py
  - tests/harness/test_harness_home.py
  - tests/harness/test_scheduler_stop.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Tests never use this computer's real agent ledger, outcomes log or free memory -->

# Goal

The test suite gives the same result whether or not real harness runs are active on the computer.

# Background

On Nov 3 2026 S9-T1's verification failed 6 scheduler tests (`test_scheduler_stop.py`, `test_scheduler_memory.py`) that wait for a task to start. Tests that build a `Scheduler` without `ledger=` and `free_memory_gb=` use the real machine-wide ledger `~/.harness/agents.json` (it held the 4 live S9 agents) and the real free memory, so their scheduler refused to start tasks while the computer was busy. S9-T6's `~/.harness/outcomes.jsonl` has the same problem: tests can write into the user's real log.

# Decisions (do not reopen)

1. New `harness/home.py`: `harness_home()` returns `Path(os.environ["HARNESS_HOME"])` when set, else `Path.home() / ".harness"`. `LedgerStore` (default folder) and `harness/outcomes.py` use it. No other behavior change.
2. `tests/conftest.py` gets autouse fixtures: `HARNESS_HOME` points to a fresh temporary folder per test; `harness.scheduler.default_free_memory_gb` (the name the scheduler falls back to when `free_memory_gb` is not passed) returns 64.0. Tests that pass their own ledger, folder or `free_memory_gb` keep working unchanged.
3. In `tests/harness/test_scheduler_stop.py`, waits for a task to start or finish use at least 30 seconds (they return as soon as the event fires, so passing runs are not slower).

# Acceptance criteria

1. A test proving that, with a fake "real" ledger full of agents in the user's home, a default-built `Scheduler` in the test suite still starts tasks.
2. Running the suite leaves `~/.harness/agents.json` and `~/.harness/outcomes.jsonl` untouched (test with `Path.home` patched to a temporary folder and checking nothing is created there).
3. `HARNESS_HOME` unset gives `~/.harness` exactly as before.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
