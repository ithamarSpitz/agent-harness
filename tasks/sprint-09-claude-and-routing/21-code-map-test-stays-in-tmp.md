---
id: S9-T21
stage: 3
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - tests/harness/test_code_map.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- The code-map timeout test never writes into the real checkout -->

# Goal

Running the test suite never changes `graphify-out/` in the checkout or worktree the tests run from.

# Background

`test_code_map_timeout_stops_process_tree` in `tests/harness/test_code_map.py` starts a fake graphify whose child process writes `late` to `Path.cwd() / 'graphify-out' / 'graph.json'` after 1.5 s. Under load the child writes before it is stopped, and its working folder is the folder pytest runs in. On Nov 3 2026 every S9 worktree and the main checkout had a 4-byte `graph.json` containing `late` after their test runs, so reviewers and `harness/impact.py` were offered a broken code map.

# Decisions (do not reopen)

1. The fake child writes only under `tmp_path` (an absolute path built into the script), never relative to its working folder, and is started with `cwd=tmp_path`.
2. The test still proves the child process is stopped: it asserts the child is not alive, and that the `late` file under `tmp_path` was not written when the stop happened in time, without failing on a slow machine where it was written before the stop.

# Acceptance criteria

1. After running `python -m pytest -q tests/harness/test_code_map.py` from a checkout, `graphify-out/graph.json` there is byte-for-byte unchanged (state the evidence).
2. The test passes 5 times in a row.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
