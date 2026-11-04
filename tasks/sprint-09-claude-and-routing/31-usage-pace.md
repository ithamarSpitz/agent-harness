---
id: S9-T31
stage: 7
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S9-T23
allowed_paths:
  - harness/pace.py
  - harness/headroom.py
  - tests/harness/test_pace.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- The harness knows how fast it can burn each usage window (input for "use it before it goes to waste") -->

# Goal

For every provider window (for example Codex 5-hour, agy Gemini weekly), the harness can tell its "full pace": how many percent per hour that window went up at its fastest recently. S9-T32 uses it to find quota that would go to waste.

# Background

`harness/headroom.py` `headroom_snapshot` reads live usage per provider window (`Headroom`: provider, used percent, resets_at, window, known) and caches it under `harness_home()`. Nothing keeps history, so the harness cannot tell how fast a window is used. On Nov 3 2026 Codex's 5-hour window went from 18% to 96% in about 2.3 hours (about 30% per hour with 2 agents).

# Decisions (do not reopen)

1. Each time `headroom_snapshot` reads fresh data (not from cache), it appends one line per known window to `harness_home() / "headroom-history.jsonl"`: `{"time": <unix>, "provider", "window", "used_percent", "resets_at"}`. Lines older than 24 hours are dropped when the file is rewritten (rewrite at most once per hour; append otherwise). Write failures never escape (log a warning).
2. New `harness/pace.py`: `full_pace(provider, window, *, now, history_path=None, defaults=None) -> float` (percent per hour):
   - Take samples for that provider and window from the last 24 hours, in time order.
   - For each pair of consecutive samples at least 10 minutes apart with the same `resets_at` (a changed `resets_at` or a drop in used percent is a reset; skip that pair), rate = (used2 - used1) / hours between them.
   - Full pace = the highest such rate. If there is no positive rate, use the default.
3. Defaults (percent per hour), used when there is no history: 5-hour windows 30, weekly windows 3, monthly windows 1, anything else 3. `defaults` can override them by window name.
4. Also `waste(left_percent, hours_to_reset, pace) -> float` = `max(0, left_percent - hours_to_reset * pace)`.
5. Window names are compared through the existing canonical window names in `harness/routing.py` if one exists; otherwise by lower-case name.

# Acceptance criteria

1. History with Codex 5-hour samples 18% at 22:30, 48% at 23:30 and 96% at 00:50 (same reset): full pace = 36 (the 48 to 96 pair).
2. A reset between samples (used drops or `resets_at` changes) is skipped, not counted as negative or huge.
3. No history: 30 for a 5-hour window, 3 for weekly.
4. `waste(80, 1, 30) == 50`, `waste(30, 3, 30) == 0`, `waste(70, 10, 3) == 40`.
5. History older than 24 hours is ignored and later dropped; a broken history line is skipped.
6. Tests use `tmp_path` and injected time; nothing reads the real machine's usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
