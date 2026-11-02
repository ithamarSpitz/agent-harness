---
id: S8-T20
stage: 6
model_class: hard_worker
timeout_minutes: 55
review: true
allow_protected: false
depends_on:
  - S8-T10
allowed_paths:
  - harness/cli.py
  - harness/scheduler.py
  - harness/self_host.py
  - harness/config.py
  - tests/harness/test_self_host.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- The harness continues on its new code after each stage -->
# Goal

When the harness builds itself, a bug fixed in one stage no longer hits the next stage of the same sprint: after each stage passes, the run continues on the new code.

# Background

The harness runs from its installed copy until the sprint ends and the release reinstalls it. In S7 (Nov 2026) a bug fixed and merged in stage 3 still stopped stage 4, and the operator had to restart the run by hand from a copy of the new code.

# Decisions (do not reopen)

1. New config `self_hosting: {enabled: false}` (default off; the harness repo turns it on in its `harness.yaml`).
2. When enabled, after a stage integrates and its stage checks pass (and it is not the last stage), the scheduler starts no new work and returns a `RESTART_ON_NEW_CODE` outcome; nothing is lost (same resumable state as the stop request from S8-T10).
3. `harness/self_host.py` builds a frozen copy of the integration checkout at that commit: `python -m pip install --no-deps --target .harness/self/<short-commit> <integration checkout>` (skip if it already exists). The CLI then starts `python -P -m harness resume <sprint>` with `PYTHONPATH` set to that directory and the same environment, and exits with its exit code. Keep-awake stays active across the handoff.
4. If building the copy fails, log a warning and continue the sprint on the current code (never block the sprint on this).
5. Old frozen copies beyond the newest 3 are removed.

# Acceptance criteria

1. With fakes for pip and the child process: after stage 1 passes, the copy is built for that commit and `resume` is started with the right `PYTHONPATH`; the parent exits with the child's exit code.
2. A failing build continues on the current code with a warning.
3. Disabled (default): behavior is unchanged.
4. Only the newest 3 copies remain.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
