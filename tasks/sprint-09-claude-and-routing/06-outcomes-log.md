---
id: S9-T6
stage: 1
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/outcomes.py
  - harness/usage.py
  - tests/harness/test_outcomes.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- One outcomes log for all projects -->
# Goal

Every agent call from every project is recorded in one machine-wide log, so we can later see which models do the work well and which waste tokens.

# Background

Each project keeps its own `.harness/usage.jsonl` (for example `C:\Users\itas1\torimitor\.harness\usage.jsonl` for BookNook and `C:\Users\itas1\dev\agent-harness\.harness\usage.jsonl` for the harness itself). The installed harness program is shared, but its data is per project, so there is no combined view today.

# Decisions (do not reopen)

1. New `harness/outcomes.py`: `append_outcome(record)` appends one JSON line to `~/.harness/outcomes.jsonl` (create the folder; append with a lock so parallel processes and projects never interleave lines; Windows-safe).
2. Each usage record that `harness/usage.py` writes is also appended there, extended with: `project` (absolute project root), `sprint`, `task_id`, `model_class`, `provider`, `model`, `effort` (when known), `phase`, `attempt`, `ok`, `verification_ok` (when known), `review_verdict` and `finding_count` (for review calls, when known), tokens, `duration_seconds`, `recorded_at`. Missing values are `null`, never guessed.
3. Writing the shared log never fails the task: errors are logged once and ignored.
4. The per-project `usage.jsonl` stays exactly as it is.

# Scope note (Nov 2026)

This task writes the fields the usage records already carry; fields they do not carry yet (`sprint`, `effort`, `verification_ok`, `review_verdict`, `finding_count`) are `null` here. Filling them in the runners and the scheduler is S9-T18 (those files are outside this task's paths). Tests must prove that any of these values present in the usage record are copied to the shared log.

# Acceptance criteria

1. Two fake projects writing at the same time produce valid, complete lines in one file.
2. A record has all keys, with `null` for unknown ones.
3. An unwritable home folder does not fail the call.

# Context

The user has three $20 subscriptions: ChatGPT Plus (Codex: 5-hour and weekly windows), Cursor ($20 monthly; its own models Composer/Grok in one pool, other models in another), and Claude Pro (Claude Code: 5-hour and weekly windows). The user's own Claude Code session controls the harness and shares the same Claude limits, so harness agents must leave Claude capacity for it. Today the harness has two providers (Codex first, Cursor as per-task fallback). S9 adds Claude Code as a third provider and chooses the provider per call from live usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
