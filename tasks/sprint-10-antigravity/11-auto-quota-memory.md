---
id: S10-T11
stage: 1
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/provider_runner.py
  - tests/harness/test_auto_quota_memory.py
  - tests/harness/test_cursor_auto_when_exhausted.py
  - tests/harness/test_provider_choice.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Once Cursor Auto reports its own usage limit, stop trying it until Cursor's reset date -->

# Goal

Cursor Auto is tried at most once per Cursor billing period after it reports a usage limit, not again after every quota wait.

# Background

Since S9-T30, Auto implements first while both Cursor pools are at 100%. Auto's own credit can run out too ("You've hit your usage limit", Nov 3 2026, until the Cursor reset on Nov 26). A failed Auto call quota-disables Cursor for that task, but `_maybe_clear_temporary_disables_on_quota_wait_exit` (`harness/provider_runner.py`) clears that disable after every quota wait, and the pools still read 100%, so Auto is tried again: on Nov 4 S9-T32 and S9-T13 each paid one 8-25 second failed Auto call after every wait.

# Decisions (do not reopen)

1. When a Cursor call running model `auto` ends quota exhausted, store a machine-local meta value `cursor.auto.exhausted_until` = the Cursor windows' known reset time from the latest headroom (the later one if they differ); if no reset time is known, 24 hours from now.
2. While now is before `cursor.auto.exhausted_until`, Auto is not offered (Cursor counts as no usage when both pools are at 100%), on every path and for every task, and the routing log line says `cursor auto: out until <date>`.
3. Clearing quota disables on quota-wait exit does not clear this value. It expires by time only, or when headroom shows a Cursor reset time later than the stored one (a new billing period).
4. Named-model Cursor calls are unaffected.

# Acceptance criteria

1. Fake headroom with both pools at 100% and reset in 22 days; a fake Auto call returns a usage limit; after a quota wait the next choice does not pick Cursor Auto, and no second Auto call is made.
2. After the stored time passes (injected clock), Auto is offered again.
3. A newer Cursor reset in headroom clears it early.

# Rules for every S10 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task that records a sample may run the real `agy` once, as its brief says.
- Never read, print, log or store tokens or credentials. agy uses the user's own login; the harness only starts the official `agy` program and never reads `~/.gemini/` files.
- Never change the user's agy configuration (`~/.gemini/antigravity-cli/settings.json`, `agy mcp add`, `agy install`); per-call settings go through `--settings`.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
