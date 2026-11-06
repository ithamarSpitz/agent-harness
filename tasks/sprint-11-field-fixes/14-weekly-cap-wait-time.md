---
id: S11-T14
stage: 4
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/routing.py
  - tests/harness/test_routing.py
  - tests/harness/test_quota_wait.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Waiting for Claude's weekly cap ends when the cap rises above use, not at the weekly reset -->
# Goal

When the only provider left is Claude and it is over its weekly cap, the harness waits until the cap passes Claude's current weekly use, not until the weekly reset days later.

# Background

Seen live 2026-11-06 02:45: S11-T9 had only Claude left, Claude weekly use 76% against a cap of 72.8% (cap = 100 - 7 x days left, `claude_weekly_cap` in `harness/routing.py`). The harness chose to wait until the weekly reset on 2026-11-10 00:00 (3 days 21 hours) and slept 1440 minutes. With use flat, the cap reaches 76% about 0.46 days later (around 14:00 the same day).

Cause: in `harness/routing.py` (around line 403-412), when `_claude_block` reports the weekly cap, the code adds that window's `reset_at` to `reset_candidates`.

# Decisions (do not reopen)

1. For a weekly-cap block, the candidate time is `reset_at - (100 - used) / weekly_percent_per_day` days (the moment `claude_weekly_cap(days_left) >= used`), using the same reserve setting `claude_weekly_cap` reads. Never earlier than `now`, never later than `reset_at`.
2. Unknown weekly reset: keep today's behavior (no candidate).
3. The 5-hour cap block keeps using the 5-hour window's reset.
4. A pure function for the time is fine (for example `claude_weekly_cap_clears_at(used, reset_at, reserve, now)`); keep it in `routing.py`.

# Acceptance criteria

1. Test in `tests/harness/test_routing.py`: weekly used 76, reset in 3.875 days, 7 per day: `wait_until` is about 0.46 days from now (within a minute), not the reset.
2. Test: used 99, reset in 2 days: the wait ends at about reset - 1/7 day.
3. Test: the 5-hour block still waits for the 5-hour reset.
4. Existing tests in `test_routing.py` and `test_quota_wait.py` pass (update any that asserted the weekly reset as the wait time, and say which in your report).

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
