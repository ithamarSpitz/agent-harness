---
id: S9-T9
stage: 5
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on:
  - S9-T8
allowed_paths:
  - harness/outcomes_report.py
  - harness/cli.py
  - tests/harness/test_outcomes_import.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Import past usage records -->
# Goal

The report does not start empty: past usage records from existing projects are imported once.

# Decisions (do not reopen)

1. `harness models import <project path>...` reads each project's `.harness/usage.jsonl` and appends its records to `~/.harness/outcomes.jsonl` with `project` set and `imported: true`.
2. Importing the same project twice adds nothing new (deduplicate by project + task + recorded_at + phase + attempt).
3. Old records keep only the fields they have; missing ones are `null`.

# Acceptance criteria

1. Importing two fake projects adds all their records; importing again adds none.
2. The report counts imported records.

# Context

The user has three $20 subscriptions: ChatGPT Plus (Codex: 5-hour and weekly windows), Cursor ($20 monthly; its own models Composer/Grok in one pool, other models in another), and Claude Pro (Claude Code: 5-hour and weekly windows). The user's own Claude Code session controls the harness and shares the same Claude limits, so harness agents must leave Claude capacity for it. Today the harness has two providers (Codex first, Cursor as per-task fallback). S9 adds Claude Code as a third provider and chooses the provider per call from live usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
