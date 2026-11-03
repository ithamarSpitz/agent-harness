---
id: S8-T11
stage: 7
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S8-T3
  - S8-T4
  - S8-T20
allowed_paths:
  - harness/cli.py
  - harness/board_loop.py
  - tests/harness/test_board_loop.py
  - tests/harness/test_cli_watch.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Show the board in run, resume and watch -->
# Goal

The live board appears in the terminal that runs the sprint, and in any other terminal through `harness watch`.

# Decisions (do not reopen)

1. `harness/board_loop.py`: a loop that builds the snapshot (S8-T3) from the state, the roadmap, the S7 added tasks, the events (S8-T10) and the parallel values (free memory from S8-T1, estimate from S8-T8, running count), and draws it with `Screen` (S8-T4) every 2 seconds and on every state change.
2. `harness run` and `harness resume`: when stdout is a TTY and ANSI can be enabled, run the board loop on a thread and suppress the progress lines; otherwise print exactly today's lines. New flag `--no-board` forces the plain lines.
3. `harness watch [sprint]`: read-only board, default sprint = the one with an active run; refreshes until Ctrl+C or the sprint ends; never writes state. Wrap it in `KeepAwake(runtime.config.execution.keep_awake_during_run)` like `run-task` (S8-T32), so a long watch keeps the computer awake.
4. `harness status` gets the one-line `parallel ...` summary while a sprint runs.
5. Unicode symbols only when the stream supports them; colors off with `NO_COLOR`.

# Acceptance criteria

1. Non-TTY `run` output is byte-identical to before (fake runner).
2. `watch` on a state file written by a fake run draws the board and leaves the file unchanged.
3. `--no-board` gives the plain lines on a TTY (fake TTY).
4. With a fake `KeepAwake`, `watch` enters and exits it once.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
