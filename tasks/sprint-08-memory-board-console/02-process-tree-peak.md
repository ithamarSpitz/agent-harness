---
id: S8-T2
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/proc_tree.py
  - tests/harness/test_proc_tree.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Peak memory of a process tree -->
# Goal

A small sampler that measures the peak memory of a process and all its children, so the harness can learn how much memory one agent of a project really uses.

# Decisions (do not reopen)

1. `harness/proc_tree.py`:
   - `list_processes() -> list[ProcInfo]` (pid, parent pid, working-set bytes). Windows: one PowerShell call `Get-CimInstance Win32_Process | Select ProcessId,ParentProcessId,WorkingSetSize | ConvertTo-Json`. POSIX: `ps -A -o pid=,ppid=,rss=` (rss in KiB).
   - `tree_bytes(root_pid, processes) -> int`: sum for the root and every descendant (pure function).
   - `PeakSampler(root_pid, interval_seconds, lister=list_processes)`: `start()`, `stop() -> peak_gb`, sampling on a daemon thread; a failed sample is skipped, never raised.
2. The lister is injectable; tests use fake process tables.

# Acceptance criteria

1. `tree_bytes` handles nested children, missing parents and pid reuse cycles (no infinite loop).
2. The Windows JSON and POSIX `ps` parsers return the right rows for fixed samples (including a single-object JSON result).
3. `PeakSampler` with a fake lister returning growing then shrinking trees reports the maximum; `stop()` returns within one interval.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
