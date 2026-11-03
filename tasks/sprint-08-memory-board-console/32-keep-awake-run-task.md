---
id: S8-T32
stage: 4
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S8-T31
allowed_paths:
  - harness/cli.py
  - tests/harness/test_keep_awake_cli.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- run-task and watch keep the computer awake too -->
# Goal

`harness run-task` keeps the computer awake like `run` and `resume` already do. (`harness watch` does not exist yet; S8-T11 creates it and wraps it in `KeepAwake`.)

# Background

`cmd_run` wraps the live run in `KeepAwake(runtime.config.execution.keep_awake_during_run)` (`harness/cli.py`). `cmd_run_task` (used for `run-task`, including `--fresh-budget` recoveries that can take an hour) and `watch` do not, so the computer can go to sleep during them.

# Decisions (do not reopen)

1. Wrap the live part of `run-task` in the same `KeepAwake(runtime.config.execution.keep_awake_during_run)`.
2. Dry runs and read-only commands (`status`, `limits`, `usage`) do not.

# Acceptance criteria

1. With a fake `KeepAwake`, `run-task` enters and exits it once; `status` does not.
2. `keep_awake_during_run: false` passes `False`.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
