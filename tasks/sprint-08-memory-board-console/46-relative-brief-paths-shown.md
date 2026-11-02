---
id: S8-T46
stage: 5
model_class: worker
timeout_minutes: 25
review: true
allow_protected: false
depends_on:
  - S8-T36
  - S8-T32
allowed_paths:
  - harness/cli.py
  - harness/plan_change.py
  - tests/harness/test_relative_brief_paths.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

Places that show or record a task's brief path keep it relative to the project root, now that `task.file` is absolute.

# Background

S8-T36 (Nov 2026) made the roadmap loader store each task's brief as an absolute path, so agents can always open it. Its report listed two places outside its allowed paths that now show the absolute path: `cmd_inspect` in `harness/cli.py` (`"file": task.file.as_posix()`) and `_task_signature` in `harness/plan_change.py` (the same). The plan-change signature is compared with signatures recorded earlier (relative paths), so an absolute path there would look like a changed task.

# Decisions (do not reopen)

1. Both places write the path relative to the project root (posix slashes), as before S8-T36. If a path is not under the project root, write it as it is.
2. Use one small helper for this (in `harness/cli.py` or `harness/plan_change.py`, whichever imports cleanly into the other); do not copy the logic.

# Acceptance criteria

1. `harness inspect <task>` output shows `tasks/...` relative paths.
2. A plan-change signature built after S8-T36 equals one recorded with the relative path for the same unchanged task.
3. Existing tests pass.
