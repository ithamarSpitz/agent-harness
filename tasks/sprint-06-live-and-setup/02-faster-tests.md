---
id: S6-T2
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - tests/harness/conftest.py
  - tests/conftest.py
  - pyproject.toml
  - requirements-harness-dev.txt
context: []
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

The full test run (`python -m pytest -q tests/harness`, about 129 tests) takes about 1 minute 50 seconds and runs at every stage merge. Bring it under 60 seconds on this machine (Windows, several CPU cores) without removing, skipping or weakening any test.

# Decisions (do not reopen)

1. Run tests in parallel with `pytest-xdist`, added to the `dev` extras in `pyproject.toml` and to `requirements-harness-dev.txt`, pinned to `pytest-xdist==3.8.0` (already installed for the system Python the harness uses). Turn it on by default through pytest options in `pyproject.toml` (`addopts = "-n auto"` or a fixed worker count if `auto` is slower here), so `python -m pytest -q tests/harness` is parallel with no extra flags.
2. If some tests cannot run in parallel (shared files, fixed ports, global environment changes), fix their isolation in `conftest.py` fixtures (unique temp directories, free ports) rather than marking them serial. Do not edit test files themselves; if a test cannot be isolated from `conftest.py`, report it in OPEN_ISSUES instead.
3. Do not touch anything under `harness/`.

# Acceptance criteria

1. The same number of tests pass as before, none skipped that ran before.
2. Three consecutive full runs pass (no flaky failures from parallelism); report the three durations in TEST_RESULTS.
3. Each run finishes in under 60 seconds.
