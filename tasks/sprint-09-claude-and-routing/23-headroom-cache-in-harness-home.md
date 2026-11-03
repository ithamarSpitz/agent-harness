---
id: S9-T23
stage: 3
model_class: worker
timeout_minutes: 25
review: true
allow_protected: false
depends_on:
  - S9-T17
allowed_paths:
  - harness/headroom.py
  - tests/harness/test_headroom.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- The headroom cache lives in the harness home folder -->

# Goal

The headroom cache file follows `HARNESS_HOME` like every other machine-wide harness file, so tests never touch the real one.

# Background

S9-T17 added `harness_home()` (`harness/home.py`: `HARNESS_HOME` or `~/.harness`) and moved the agent ledger and the shared outcomes log onto it. `headroom_snapshot` in `harness/headroom.py` (S9-T4) still builds its default cache path as `Path.home() / ".harness" / "headroom-cache.json"`.

# Decisions (do not reopen)

1. The default cache path is `harness_home() / "headroom-cache.json"`, computed at call time (not at import), so an environment change is seen.
2. An explicit `cache_path` argument still wins. No other change.

# Acceptance criteria

1. With `HARNESS_HOME` set to a temporary folder, a snapshot with fake providers writes the cache there and nothing under the real home folder.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
