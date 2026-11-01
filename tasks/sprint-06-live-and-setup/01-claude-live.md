---
id: S6-T1
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/limits.py
  - tests/harness/test_limits.py
context: []
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

`harness limits` shows Claude's live plan usage instead of the status-line file, which only updates when Claude Code replies and so lags behind (it showed 17% / 7% while the real values were 20% / 8%).

# Facts (verified 2026-11-01)

- Claude Code keeps its login in `~/.claude/.credentials.json`: `{"claudeAiOauth": {"accessToken": "...", "expiresAt": <Unix milliseconds>, "subscriptionType": "...", "rateLimitTier": "...", ...}}`. Claude Code refreshes the token itself when it runs.
- `GET https://api.anthropic.com/api/oauth/usage` with headers `Authorization: Bearer <accessToken>`, `anthropic-beta: oauth-2025-04-20` returns, among many other keys:

  ```text
  {"five_hour":  {"utilization": 21.0, "resets_at": "2026-11-01T23:00:00.254455+00:00", ...},
   "seven_day":  {"utilization": 8.0,  "resets_at": "2026-11-02T22:00:00.254477+00:00", ...},
   "seven_day_opus": null, "seven_day_sonnet": null,
   "extra_usage": {"is_enabled": false, ...}}
  ```
  `utilization` is a percent; `resets_at` is ISO 8601 with a time zone. Any window may be null.

# Decisions (do not reopen)

1. **Live first.** Read `accessToken` and `subscriptionType`; send the request above with `urllib.request`, timeout 20 seconds. Use `five_hour` and `seven_day`; also show `seven_day_opus` and `seven_day_sonnet` when they are not null. Show `Extra usage: enabled` only when `extra_usage.is_enabled` is true.
2. **Fallback.** If the credentials file is missing, the token is past `expiresAt`, or the request fails (401, network, timeout, bad JSON), use today's status-line file and add `(from last Claude Code reply, may be stale)` to its "as of" line. Never refresh or write the token; never call any other endpoint.
3. **Show** (live), same style as the other services:
   ```text
   Claude (<subscriptionType>)
     5-hour   21% used, resets Mon Nov 02, 01:00 AM (in 2h 39m)
     weekly    8% used, resets Tue Nov 03, 12:00 AM (in 1d 1h)
   ```
   Round to whole percents; local times; the past-reset rule stays.
4. **The token is a secret:** never print, log or store it, never include it in exceptions or `--json`, and send it only to `api.anthropic.com`.
5. `--json` gets `source: "live"` or `"status-line"` for Claude. Codex and Cursor sections do not change.

# Acceptance criteria

Tests use a temp home directory, a fake credentials file and a monkeypatched HTTP call; no real network.

1. Live values are shown with the subscription type; null windows are skipped; Opus/Sonnet windows appear when present.
2. Missing file, expired token, HTTP 401 and timeout each fall back to the status-line file with the "may be stale" note.
3. The fake token never appears in text output, `--json` output or raised messages, and is sent only to `api.anthropic.com`.
