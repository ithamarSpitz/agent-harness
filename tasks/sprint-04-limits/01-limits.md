---
id: S4-T1
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/**
  - tests/harness/**
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

`harness limits` prints the user's current usage and limits for Codex, Cursor and Claude in one short view, each in the way that matters for that service. It works from any directory, inside or outside a project.

# Decisions (do not reopen)

1. **Command.** `harness limits [--json]`, in a new module `harness/limits.py`, wired into `harness/cli.py`. Like `harness init`, it must not require `harness.yaml` or a roadmap: dispatch it before any project runtime is built.
2. **Independent sections.** Each service is read separately. A failure in one prints `<Service>: unavailable (<short reason>)` for that service only; the others still print. The command always exits 0.
3. **Codex (5-hour and weekly windows).**
   - Source: the newest file by modification time matching `<CODEX_HOME or ~/.codex>/sessions/**/rollout-*.jsonl`; in it, the last line whose JSON is `{"type": "event_msg", "payload": {"type": "token_count", "rate_limits": {...}}}`.
   - `rate_limits.primary` is the 5-hour window and `rate_limits.secondary` the weekly window: each has `used_percent`, `window_minutes`, `resets_at` (Unix seconds). Also `plan_type` and `credits` (`has_credits`, `unlimited`, `balance`).
   - Show per window: used percent, reset as local day and time plus "in Xh Ym". If `resets_at` is already in the past, show `0% (reset at <time>)` instead of the old percent.
   - Show the plan type, credits balance only when `has_credits` is true, and the data age: "as of <time of that log line> (last Codex call)". The values are only as fresh as the last Codex call; say so in that line.
4. **Cursor (monthly billing cycle).**
   - Token: read the value of key `cursorAuth/accessToken` from table `ItemTable` in Cursor's `state.vscdb`: `%APPDATA%\Cursor\User\globalStorage\state.vscdb` on Windows, `~/Library/Application Support/Cursor/User/globalStorage/state.vscdb` on macOS, `~/.config/Cursor/User/globalStorage/state.vscdb` on Linux. Read it from a temporary copy (copy the file, open the copy, delete it), never the original.
   - Request: `POST https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage` with body `{}` and headers `Authorization: Bearer <token>`, `Content-Type: application/json`, `Connect-Protocol-Version: 1`, timeout 20 seconds, using `urllib.request`.
   - Response fields used: `billingCycleEnd` (Unix milliseconds, string or number); `planUsage.autoPercentUsed` (the "Cursor Models" pool: Composer and Cursor Grok); `planUsage.apiPercentUsed` (the "Other Models" pool); `spendLimitUsage` (on-demand spending; absent or without a limit means disabled).
   - Show it the way Cursor's own Spending page (cursor.com/dashboard/spending) does, nothing else:
     ```text
     Cursor (monthly)  resets <billingCycleEnd as local date> (<N> days left)
       Cursor models (Composer, Grok)  19% used
       Other models                     0% used
       On-demand spending              disabled
     ```
     Round percents to whole numbers. Do not show dollar amounts, `includedSpend`, `totalPercentUsed` or `displayMessage`: they do not match what the Spending page shows.
   - **The token is a secret**: never print, log, store or include it in exceptions, `--json` output or test output. Send it only to `api2.cursor.sh`.
5. **Claude (5-hour and weekly windows).**
   - Source: `~/.claude/usage-cache.json`, written by the user's Claude Code status line: `{"saved_at": <Unix seconds>, "rate_limits": {"five_hour": {"used_percentage": n, "resets_at": <Unix seconds>}, "seven_day": {...}}}`. Any window may be missing.
   - Show per window: used percent and reset like Codex, with the same past-reset rule, plus "as of <saved_at> (last Claude Code reply)".
   - When the file is missing: `Claude: no data yet (Claude Code saves it on its next reply)`.
6. **Output.** Plain text, one block per service in the order Codex, Cursor, Claude, at most about 4 lines each, local times. `--json` prints one JSON object with a key per service containing the parsed numbers (or an `error` string), and no token.
7. **No new dependencies.** Standard library only (`json`, `sqlite3`, `urllib`, `shutil`, `tempfile`, `datetime`). Paths come from functions that tests can point at temp directories; `CODEX_HOME` is honored.
8. **README.** A short "Usage and limits across services" section: what each line means, where the data comes from, that Codex data is as of the last Codex call, that Claude needs the status line (`~/.claude/statusline-usage.py`), and that Cursor's endpoint is the one the Cursor app uses and is undocumented.

# Out of scope

Changing `harness usage`, `quota status` or how runs wait for quota; using these numbers to schedule work; installing the Claude status line; any network call except the one Cursor request.

# Acceptance criteria

Tests use temp directories, a fake `state.vscdb` (sqlite) and a monkeypatched HTTP call; no real network.

1. Codex: picks the newest session file and its last `rate_limits` line; formats both windows; a past `resets_at` shows `0% (reset at ...)`; no session files gives `Codex: unavailable (...)`.
2. Cursor: sends exactly the request in decision 4; formats dollars, both pools and days left; HTTP 401 or a missing database gives `Cursor: unavailable (...)`; the fake token string never appears in text output, `--json` output or raised messages.
3. Claude: formats both windows and the age; missing file gives the "no data yet" line; a missing window is skipped.
4. One failing service does not stop the others; exit code 0.
5. `harness limits` runs in a directory with no `harness.yaml`.
