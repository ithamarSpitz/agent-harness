---
id: S8-T33
stage: 1
model_class: hard_worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/agent_ledger.py
  - tests/harness/test_agent_ledger.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

One machine-wide record of every agent that any harness on this computer is running, so harnesses of different projects (for example BookNook and the harness repository, run from different terminals) can share memory instead of each acting as if it were alone.

# Background

On Nov 2 2026 two harness runs (BookNook S6 and agent-harness S8, three agents) plus the user's apps left about 3 GB free of 16 GB. Agent launches then failed with Windows error 0xC0000142 and tasks were blocked. Each harness only knows its own agents.

# Decisions (do not reopen)

1. New `harness/agent_ledger.py`, data in `~/.harness/agents.json` (create the folder).
2. Records:
   - agent: `project` (absolute root), `task_id`, `harness_pid`, `estimate_gb`, `started_at`;
   - waiting: `project`, `harness_pid`, `ready_tasks` (how many tasks it could start now), `since`.
3. API: `register_agent(...) -> id`, `unregister_agent(id)`, `set_waiting(project, harness_pid, ready_tasks)` (0 removes the record), `snapshot() -> Ledger` (agents and waiting records).
4. Every read drops records whose `harness_pid` is no longer alive (a crashed harness never holds memory forever). Process-alive check: Windows `OpenProcess` through `ctypes`, POSIX `os.kill(pid, 0)`; injectable for tests.
5. Every change is a read-modify-write under a cross-process file lock (`msvcrt.locking` on Windows, `fcntl.flock` on POSIX) on `~/.harness/agents.lock`, with the JSON written to a temp file and renamed. A lock wait over 10 seconds or a corrupt file logs a warning and starts from an empty ledger; the ledger never fails a run.
6. The ledger folder is injectable (tests use a temp folder).

# Acceptance criteria

1. Two processes registering and unregistering in parallel (real subprocesses in a test, temp folder) leave a correct, valid file.
2. Records of a dead `harness_pid` disappear on the next read.
3. A corrupt file is replaced with an empty ledger and a warning.
