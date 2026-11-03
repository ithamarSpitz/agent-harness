---
id: S9-T7
stage: 2
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S9-T6
allowed_paths:
  - harness/scheduler.py
  - tests/harness/test_task_final_outcome.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Record how each task finally passed -->
# Goal

When a task is merged (`DONE`) or `BLOCKED`, one summary line in the shared outcomes log says how it got there.

# Decisions (do not reopen)

1. On `DONE` and on `BLOCKED`, append (through `harness.outcomes.append_outcome`) a record with `event_type: "task_final"`: project, sprint, task, model class, final status, number of implementation attempts, the provider/model/effort of every attempt in order, the provider/model/effort that produced the passing attempt (for `DONE`), total tokens and total duration across the task's calls.
2. Values come from the task runtime and the usage records already kept; missing values are `null`.
3. Never fails the task.

# Acceptance criteria

1. A fake task that passes on attempt 2 after a Sonnet-medium failure records both attempts and the passing one.
2. A blocked task records `BLOCKED` and all attempts.

# Context

The user has three $20 subscriptions: ChatGPT Plus (Codex: 5-hour and weekly windows), Cursor ($20 monthly; its own models Composer/Grok in one pool, other models in another), and Claude Pro (Claude Code: 5-hour and weekly windows). The user's own Claude Code session controls the harness and shares the same Claude limits, so harness agents must leave Claude capacity for it. Today the harness has two providers (Codex first, Cursor as per-task fallback). S9 adds Claude Code as a third provider and chooses the provider per call from live usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
