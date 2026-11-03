---
id: S9-T12
stage: 5
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on:
  - S9-T10
allowed_paths:
  - harness/board.py
  - tests/harness/test_board.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- The board shows the chosen provider and why -->
# Goal

The live board (S8) shows, for each running agent, which provider and model it is on and the one-line reason it was chosen.

# Decisions (do not reopen)

1. `BoardSnapshot` task rows get an optional `routing_reason`; the row shows provider/model as today and, on a second indented line, the reason, cut to the width.
2. The header shows each provider's headroom in one short line, for example `codex 5h 100% (17:14) · claude 5h 58% wk 22% · cursor pool 100%`.

# Acceptance criteria

1. Fixed snapshots render the reason line and the headroom line within widths 60 and 120.
2. Snapshots without the new fields render exactly as before.

# Context

The user has three $20 subscriptions: ChatGPT Plus (Codex: 5-hour and weekly windows), Cursor ($20 monthly; its own models Composer/Grok in one pool, other models in another), and Claude Pro (Claude Code: 5-hour and weekly windows). The user's own Claude Code session controls the harness and shares the same Claude limits, so harness agents must leave Claude capacity for it. Today the harness has two providers (Codex first, Cursor as per-task fallback). S9 adds Claude Code as a third provider and chooses the provider per call from live usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
