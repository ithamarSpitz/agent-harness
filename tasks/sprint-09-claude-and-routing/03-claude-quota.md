---
id: S9-T3
stage: 3
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S9-T2
allowed_paths:
  - harness/claude_runner.py
  - tests/harness/test_claude_quota.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Claude Code usage limits and errors -->
# Goal

When Claude Code hits a usage limit or a temporary error, the harness reacts the same way it does for Codex and Cursor.

# Decisions (do not reopen)

1. Classify only failed calls (non-zero exit, `is_error: true`, timeout, stall); never pattern-match a successful answer.
2. Usage limit (texts such as `usage limit`, `limit reached`, `rate_limit`, `rate limit`, HTTP 429): the provider is out of quota for this call; the caller falls back or waits exactly as for the other providers.
3. Network texts → `WAITING_NETWORK`; overload (`overloaded`, HTTP 529) → `WAITING_PROVIDER_BUSY`; 5xx / `api_error` → `WAITING_PROVIDER_ERROR`; timeout or stall → `WAITING_TIMEOUT`; login problems (`not logged in`, `authentication`, `/login`) → an auth failure like Codex auth failures.
4. Keep the pattern lists in `claude_runner.py` as constants that config may extend later; put each list in one place.

# Acceptance criteria

1. Table-driven tests: each sample error output (failed call) maps to its cause.
2. A successful answer that mentions "rate limit" or "overloaded" stays a success.

# Context

The user has three $20 subscriptions: ChatGPT Plus (Codex: 5-hour and weekly windows), Cursor ($20 monthly; its own models Composer/Grok in one pool, other models in another), and Claude Pro (Claude Code: 5-hour and weekly windows). The user's own Claude Code session controls the harness and shares the same Claude limits, so harness agents must leave Claude capacity for it. Today the harness has two providers (Codex first, Cursor as per-task fallback). S9 adds Claude Code as a third provider and chooses the provider per call from live usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
