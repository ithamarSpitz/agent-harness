---
id: S8-T31
stage: 1
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/quota.py
  - tests/harness/test_keep_awake.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Keep the computer awake on macOS and Linux too -->
# Goal

`KeepAwake` (in `harness/quota.py`) prevents idle sleep on macOS and Linux as well as Windows.

# Background

`KeepAwake` wraps `harness run` / `resume` (`execution.keep_awake_during_run`) and capacity waits (`capacity.keep_awake`), but it returns immediately when `os.name != "nt"`, so on macOS and Linux a long unattended run can be stopped by idle sleep.

# Decisions (do not reopen)

1. Keep the Windows code path exactly as it is.
2. macOS: on the outermost enter start `caffeinate -i -w <harness pid>`; stop it on the outermost exit.
3. Linux: when `systemd-inhibit` is on PATH, start `systemd-inhibit --what=idle:sleep --why="agent harness run" sleep infinity` on the outermost enter and stop it on the outermost exit; otherwise do nothing.
4. Same nesting rule as today (only the outermost enter/exit act). Any failure is ignored with one log warning; it never fails a run.
5. Process start/stop goes through small injectable functions so tests never start real helpers.

# Acceptance criteria

1. With fakes: macOS and Linux start the helper once on nested enters and stop it once on the last exit.
2. Linux without `systemd-inhibit` does nothing.
3. Existing Windows behavior and tests are unchanged.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
