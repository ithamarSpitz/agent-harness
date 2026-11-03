---
id: S9-T19
stage: 3
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/ui_review.py
  - tests/harness/test_ui_review.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- UI cleanup never misses a worktree process on a busy computer -->

# Goal

After a UI check, every process started from the task worktree is stopped, even when the computer is busy, and a miss is never silent.

# Background

On Nov 3 2026 `test_provider_review_cleanup_stops_worktree_background_process` failed during S9-T2's verification on a busy computer (it passes alone): no cleanup log was written, and a leaked `python -c "import time; time.sleep(3600)"` process from that test was later found still running. On Windows `_collect_processes_for_worktree` in `harness/ui_review.py` starts `powershell.exe` with a CIM query and a 30 s timeout; on `TimeoutExpired` or `OSError` it silently returns no processes, so nothing is stopped and nothing is logged. In real runs the same miss would leave a dev server running after a review.

# Decisions (do not reopen)

1. On Windows, give the CIM query a 90 s timeout and retry it once after a failure or timeout. Also stop processes that descend from the agent call's own process: keep the agent's PID and start time from the call (the PID hook the runners already report), and match a process whose parent chain reaches that PID even though the agent itself has exited (Windows keeps `ParentProcessId` of an exited parent), accepting only processes created after the agent started (guards against PID reuse). Never match by the harness's own PID: other tasks' agents are its children too and must never be stopped. `cleanup_ui_review_processes` gets optional `agent_pid` and `agent_started_at` parameters; when they are not given, only the worktree-path match runs (as today). Passing them from `harness/provider_runner.py` is S9-T20's job (that file is not in this task's allowed_paths); test the ancestry match by calling the function directly with a fake process list where the agent PID is absent.
2. If the process lookup fails or times out, write one line to the task's `ui-review-cleanup.log` saying so (never raise, never fail the review).
3. In the test, the fake reviewer writes the background process id to a file; the test asserts that exact process is stopped, and a `finally` always kills it so the test never leaks a process.

# Acceptance criteria

1. A fake lookup that times out produces the log line and does not raise.
2. The cleanup test passes 10 times in a row while another `pytest -q tests/harness` runs (state the evidence), and leaves no `time.sleep(3600)` process behind.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
