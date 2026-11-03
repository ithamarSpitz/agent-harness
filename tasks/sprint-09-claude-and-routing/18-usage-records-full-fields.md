---
id: S9-T18
stage: 3
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S9-T6
  - S9-T7
allowed_paths:
  - harness/usage.py
  - harness/codex_runner.py
  - harness/runner.py
  - harness/scheduler.py
  - tests/harness/test_usage_fields.py
  - tests/harness/test_cli_end_to_end.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Usage records carry sprint, effort, verification and review results -->

# Goal

The shared outcomes log (S9-T6) gets real values for `sprint`, `effort`, `verification_ok`, `review_verdict` and `finding_count` instead of `null`.

# Background

S9-T6 (Nov 2026) appends every usage record to `~/.harness/outcomes.jsonl`, but the usage records written by `harness/codex_runner.py` (`_record_usage`) and `harness/runner.py` (`_record_usage`) do not include those fields, and the verification result and review verdict are only known later in `harness/scheduler.py`. So the log cannot be used to compare models yet.

# Decisions (do not reopen)

1. The runners include `sprint` (when the runner knows the task's sprint) and `effort` (Codex: the reasoning effort passed on the command line; Cursor: `null`) in every usage record.
2. Review calls include `review_verdict` (`PASS`/`FAIL`, parsed with the same parser the scheduler uses) and `finding_count` (number of top-level finding items after a FAIL; 0 for PASS).
3. After verification of an attempt, the scheduler appends one usage-style record with `phase: "verification"`, `verification_ok`, the task, attempt and duration, through the same `harness/usage.py` path, so it reaches both logs.
4. The per-project `usage.jsonl` keeps all existing keys; new keys are only added.

# Note (Nov 3 2026)

The new `phase: "verification"` record adds a line to `.harness/usage.jsonl`; `tests/harness/test_cli_end_to_end.py` counts usage lines and expects 2. Update that expectation (count call records and the verification record separately) instead of changing the new behavior.

# Acceptance criteria

1. With fake runners, an implement record has `sprint` and Codex `effort`; a review record has `review_verdict` and `finding_count`; a verification record has `verification_ok`.
2. Existing usage readers (`harness usage`, `harness limits`, the status line) still work with the extra keys.

# Context

The user has three $20 subscriptions: ChatGPT Plus (Codex: 5-hour and weekly windows), Cursor ($20 monthly; its own models Composer/Grok in one pool, other models in another), and Claude Pro (Claude Code: 5-hour and weekly windows). The user's own Claude Code session controls the harness and shares the same Claude limits, so harness agents must leave Claude capacity for it. Today the harness has two providers (Codex first, Cursor as per-task fallback). S9 adds Claude Code as a third provider and chooses the provider per call from live usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
