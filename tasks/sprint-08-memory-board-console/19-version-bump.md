---
id: S8-T19
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - scripts/bump_version.py
  - tests/harness/test_bump_version.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Bump the version at release -->
# Goal

Each successful harness sprint release raises the version, so `harness.__version__` tells which build is installed.

# Decisions (do not reopen)

1. `scripts/bump_version.py [--part minor|patch]` (default `minor`): reads `__version__ = "X.Y.Z"` in `harness/__init__.py`, writes the bumped value (minor: X.(Y+1).0, patch: X.Y.(Z+1)), and prints the new version. It only edits that file; committing is done by the release commands.
2. Exit with a clear message if the version line is missing or malformed; never write a partial file.
3. The operator adds it to `release.commands` in `harness.yaml` (not part of this task).

# Acceptance criteria

1. `0.16.0` becomes `0.17.0` (minor) and `0.16.1` (patch) on a temp copy.
2. A missing or malformed version line exits non-zero and leaves the file unchanged.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
