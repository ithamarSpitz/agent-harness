---
id: S5-T2
stage: 2
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S5-T1
allowed_paths:
  - harness/limits.py
  - tests/harness/test_limits.py
  - README.md
context:
  - readme
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

`harness limits` shows Codex's live limits instead of the last values written to a session log. The session log is stale: it showed 34% used while Codex had already refused work at 100%.

# Facts (verified with codex-cli 0.166.0)

`codex app-server` speaks JSON-RPC, one JSON object per line on stdin/stdout. This exchange returns live limits and spends no tokens:

```text
-> {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "harness-limits", "title": "harness limits", "version": "<harness version>"}}}
<- {"id": 1, "result": {...}}
-> {"method": "initialized"}
-> {"id": 2, "method": "account/rateLimits/read"}
<- {"id": 2, "result": {
     "ordinaryUsageAllowed": false,
     "rateLimits": {
       "primary":   {"usedPercent": 100, "windowDurationMins": 300,   "resetsAt": 1793570138},
       "secondary": {"usedPercent": 16,  "windowDurationMins": 10080, "resetsAt": 1794156938},
       "credits": {"hasCredits": false, "unlimited": false, "balance": "0"},
       "planType": "plus",
       "rateLimitReachedType": "rate_limit_reached"   (null when not reached)
     },
     "rateLimitResetCredits": {"availableCount": 2, "credits": [
       {"status": "available", "title": "Full reset (Weekly + 5 hr)", "expiresAt": 1794003545, ...}, ...]}
   }}
```

Other lines (notifications) may arrive in between; match the response by `id`.

# Decisions (do not reopen)

1. **Live first.** Start `codex app-server` (command `codex`, resolved like the harness resolves other commands on Windows), do the exchange above, read the `id: 2` response, then stop the process and its children with `stop_process_tree`. Overall timeout 20 seconds.
2. **Fallback.** If the live call fails for any reason (command missing, timeout, error response), use today's session-log reading and add `(from last Codex call, may be stale)` to its "as of" line.
3. **Show** (live):
   ```text
   Codex (plus)  LIMIT REACHED            <- only when rateLimitReachedType is set or ordinaryUsageAllowed is false
     5-hour   100% used, resets Sun Nov 01, 11:55 PM (in 1h 34m)
     weekly    16% used, resets Sun Nov 08, 06:55 PM (in 6d 20h)
     reset credits: 2 available (Full reset (Weekly + 5 hr): expires Nov 07; Full reset (Weekly + 5 hr): expires Nov 30)
   ```
   Show the credits balance only when `hasCredits` is true; show the reset-credits line only when `availableCount` > 0, listing only credits with `status` `available`. Durations of 24 hours or more are written as `Nd Nh`. The past-reset rule from S4 stays.
4. `--json` includes the live fields: both windows, `limit_reached`, `reset_credits` (title and expiry), and `source: "live"` or `"session-log"`.
5. Nothing else changes: Cursor and Claude sections, exit code 0, one failing service never stops the others.

# Acceptance criteria

Tests use a fake `codex` command (a small Python script speaking the exchange above, with variants that hang, exit, or return an error) and no real network:

1. Live data is shown, including LIMIT REACHED and the reset-credits line.
2. Hang, crash or error response falls back to the session log with the "may be stale" note; the fake process is stopped (no leftover process).
3. Durations of 24 hours or more print as days and hours.
4. `--json` has the new fields and `source`.
