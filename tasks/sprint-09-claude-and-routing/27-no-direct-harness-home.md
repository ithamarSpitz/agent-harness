---
id: S9-T27
stage: 6
model_class: worker
timeout_minutes: 25
review: true
allow_protected: false
depends_on:
  - S9-T9
  - S9-T23
allowed_paths:
  - tests/harness/test_no_direct_harness_home.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- A guard test: machine-wide files always go through harness_home() -->

# Goal

No harness module builds a path to the machine-wide harness folder on its own; every one goes through `harness_home()` (`harness/home.py`), so `HARNESS_HOME` always applies.

# Background

`harness_home()` (S9-T17) is the one place that resolves `HARNESS_HOME` or `~/.harness`. Twice on Nov 3 2026 a module still built `Path.home() / ".harness"` itself: the headroom cache (fixed by S9-T23) and the outcomes report and import (fixed in S9-T9 after a review). Each time, tests or `HARNESS_HOME` users read and wrote different files.

# Decisions (do not reopen)

1. New test `tests/harness/test_no_direct_harness_home.py` reads every `harness/*.py` source (not importing them) and fails, naming file and line, when a line other than in `harness/home.py` contains `".harness"` together with `Path.home()` or `expanduser("~`.
2. Project-local `.harness/` folders (paths built from the project root, such as `root / ".harness"`) are allowed; the test matches only home-based constructions.

# Acceptance criteria

1. The test passes on the current code (after S9-T9 and S9-T23).
2. A temporary fake source with `Path.home() / ".harness" / "x"` (written under `tmp_path` and checked with the same function) makes it fail with the file and line.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
