---
id: S9-T8
stage: 2
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S9-T6
allowed_paths:
  - harness/outcomes_report.py
  - harness/cli.py
  - tests/harness/test_models_report.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- harness models report -->
# Goal

A command shows, across all projects, how well each model and effort does, so the ladders can be tuned with evidence.

# Decisions (do not reopen)

1. New `harness models report [--project PATH] [--since YYYY-MM-DD]` reading `~/.harness/outcomes.jsonl` (logic in `harness/outcomes_report.py`, the command in `harness/cli.py`).
2. One row per provider + model + effort + phase: calls, first-attempt pass rate (implementation calls on attempt 1 whose task's verification and review passed on that attempt), average attempts until `DONE` for tasks that started on it, tokens per `DONE` task, and how often a later attempt with a different model fixed its work.
3. A short "suggestions" list under the table, only when there are at least 10 tasks for that model: for example `sonnet/medium: 3 of 14 first attempts passed; 11 were fixed by sonnet/high or opus. Consider starting with sonnet/high.` Suggestions are text only; nothing is changed automatically.
4. Works with old records that miss fields (counted as unknown, not errors).

# Note (Nov 2026)

The shared log also holds one `event_type: "task_final"` line per finished task (S9-T7) with a different shape. Per-call statistics use only call records (no `event_type`, or one that is not `task_final`); `task_final` lines are used for the per-task pass/attempt numbers, and unknown event types are skipped, never a crash. Test with a log mixing both.

# Acceptance criteria

1. A fixed sample log produces the expected table and suggestion lines.
2. `--project` and `--since` filter correctly.
3. An empty or missing log prints a one-line message.

# Context

The user has three $20 subscriptions: ChatGPT Plus (Codex: 5-hour and weekly windows), Cursor ($20 monthly; its own models Composer/Grok in one pool, other models in another), and Claude Pro (Claude Code: 5-hour and weekly windows). The user's own Claude Code session controls the harness and shares the same Claude limits, so harness agents must leave Claude capacity for it. Today the harness has two providers (Codex first, Cursor as per-task fallback). S9 adds Claude Code as a third provider and chooses the provider per call from live usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
