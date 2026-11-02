---
id: S8-T36
stage: 2
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/roadmap.py
  - tests/harness/test_roadmap_paths.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

Every task loaded from the roadmap knows the full path of its brief, so the harness can read it no matter which folder it was started from.

# Background

Found by the S8-T17 reviewer (Nov 2026): the roadmap loader keeps `task.file` as written in `roadmap.yaml` (relative, like `tasks/sprint-08-.../17-current-brief-in-prompt.md`; see `harness/roadmap.py` near line 76). Code that opens the brief then depends on the current folder, so `harness --root <project> ...` started from another folder cannot read it.

# Decisions (do not reopen)

1. The loader resolves each task's `file` against the project root (the folder that holds the roadmap's project, the same root the rest of the harness uses) and stores the absolute path in `task.file`.
2. Anything that prints or records the brief path for people (status, logs, roadmap files written back) keeps showing it relative to the project root, as today: find those places in `harness/roadmap.py` and keep them relative; if one is outside your allowed paths, list it in OPEN_ISSUES instead of changing it.
3. Brief files added with `harness add-task` get the same treatment if they pass through the loader.

# Acceptance criteria

1. Loading a temp project's roadmap from another current folder gives absolute `task.file` paths that exist.
2. Existing roadmap and CLI tests pass.
