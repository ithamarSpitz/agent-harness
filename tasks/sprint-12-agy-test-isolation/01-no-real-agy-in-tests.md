---
id: S12-T1
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - tests/conftest.py
  - tests/harness/test_pace.py
  - tests/harness/test_no_real_agy.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Tests never run the real agy, even when harness.yaml enables it -->

# Goal

The test suite never starts the real `agy` CLI, whatever `harness.yaml` or the developer's PATH says.

# Background

Found 2026-11-06 right after 0.21: `harness.yaml` now has `agy.enabled: true`, and `tests/harness/test_pace.py::test_snapshot_records_to_harness_home` fails. It calls `headroom.headroom_snapshot(...)` with fake Codex, Cursor and Claude sources but no agy settings, so `headroom_snapshot` reads `agy.enabled` from the repo config and runs the real `agy /usage` (the result shows an extra `('agy', 100.0)` row). Real agents must never run in tests, and with agy enabled this test fails every task's verification.

`tests/conftest.py` already blocks the real Graphify the same way this task should block agy: the session fixture `isolate_graphify_command` puts a `graphify` shim that exits 1 first on PATH.

# Decisions (do not reopen)

1. In `tests/conftest.py`, extend the existing session PATH shim so the same temp bin folder also holds an `agy` shim that exits 1 (`agy.cmd` with `@exit /b 1` on Windows; `agy` with `#!/bin/sh\nexit 1` and mode 0o755 elsewhere). Keep one fixture; rename it if you like (for example `isolate_external_commands`) and update its docstring.
2. In `test_pace.py::test_snapshot_records_to_harness_home`, pass `agy_enabled=False` so the test states what it checks.
3. Do not change anything under `harness/`.

# Acceptance criteria

1. With `agy.enabled: true` in the repo `harness.yaml` (as it is now), `python -m pytest -q tests/harness` passes.
2. New test `tests/harness/test_no_real_agy.py`: inside the test session, `shutil.which("agy")` resolves to the conftest shim folder, and running it exits non-zero without output.
3. New test in the same file: `headroom_snapshot` with fake Codex/Cursor/Claude sources and `agy_enabled=True`, `agy_command="agy"`, and the default `agy_source` returns an unknown or failed agy entry (never real usage) and does not raise.

# Rules

- Standard library only. Windows, Linux and macOS.
- Tests never call real agents, real GitHub, or the network.
- Stop and report if a test outside allowed_paths fails for a reason this task cannot fix inside its allowed paths.
