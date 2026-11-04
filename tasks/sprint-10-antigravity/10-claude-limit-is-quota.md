---
id: S10-T10
stage: 1
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/claude_runner.py
  - tests/harness/test_claude_quota.py
  - tests/harness/test_claude_runner.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Claude's "hit your 5-hour limit" is a quota wait, not a provider error -->

# Goal

When Claude Code stops because the user's Claude plan limit is reached, the task waits for quota (`WAITING_QUOTA`) like Codex and Cursor do, instead of `WAITING_PROVIDER_ERROR` with a blind 5-minute retry.

# Background

On Nov 4 2026 (S9-T13 review) Claude's final stream-json result was: `"is_error":true`, `"api_error_status":429`, `"api_error":"usage_limit_reached"`, `"result":"You've hit your 5-hour limit · resets 6:50pm (Asia/Jerusalem)"`, `"terminal_reason":"api_error"`. `harness/claude_runner.py` `CLAUDE_QUOTA_PATTERNS` look for "usage limit" / "limit reached" with spaces and `"status_code": 429`, so none matched and the call was classified `provider_error`.

# Decisions (do not reopen)

1. A Claude result is quota exhaustion when any of these hold (case-insensitive): `api_error` is `usage_limit_reached` (or contains `usage_limit` / `rate_limit`), `api_error_status` is 429, or the result text contains "hit your" together with "limit" (5-hour, weekly or any other window name).
2. Match on the parsed final result fields when the stream is JSON; keep the text patterns as a fallback (add `usage_limit_reached`, `"api_error_status":429` and `"api_error_status": 429`).
3. Busy (529, overloaded), network and auth classification stay as today.
4. The quota wait uses the existing machinery (live headroom / reset time); no new config.

# Acceptance criteria

1. A recorded final result exactly like the Background sample (as a test fixture string) is classified as quota exhausted, and the task goes to `WAITING_QUOTA`, not `WAITING_PROVIDER_ERROR`.
2. A weekly-limit variant ("You've hit your weekly limit") is quota too.
3. A 529 overloaded result is still busy, a connection error still network.

# Rules for every S10 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task that records a sample may run the real `agy` once, as its brief says.
- Never read, print, log or store tokens or credentials. agy uses the user's own login; the harness only starts the official `agy` program and never reads `~/.gemini/` files.
- Never change the user's agy configuration (`~/.gemini/antigravity-cli/settings.json`, `agy mcp add`, `agy install`); per-call settings go through `--settings`.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
