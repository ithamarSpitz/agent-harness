---
id: S9-T33
stage: 6
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S9-T12
allowed_paths:
  - harness/board.py
  - tests/harness/test_board_headroom.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- The board header shows every provider's live headroom in one line that always fits -->

# Goal

The live board's header shows each provider's current usage in one line, never stale, and every known value stays visible at width 60.

# Background

Split from S9-T12 (Nov 4 2026). S9-T12's three attempts failed review on this line: values cut off at width 60 (`cursor pool 1` instead of `100%`, Cursor's second pool dropped), and the board read the headroom cache file directly without its age, so old values could show forever. `harness/headroom.py` `headroom_snapshot(max_age_seconds=300)` is the API for live headroom (it refreshes stale data and caches it).

# Decisions (do not reopen)

1. Data: `BoardSnapshot` gets an optional `headroom` filled from `headroom_snapshot()` (default max age, injectable in tests). Never read the cache file directly. A failed read means no headroom line.
2. Full format, one entry per provider in `providers.priority` order, separated by ` · `:
   - Codex: `codex 5h 96% (03:11) wk 46%` (reset clock in local time for the 5-hour window).
   - Cursor: `cursor 100%/25%` (Cursor models pool / other models pool).
   - Claude: `claude 5h 58% wk 22%`.
   - Any other provider (for example agy later): `<name> <window> <n>%` for each window, in the order the snapshot gives them.
   - Unknown value: `?` instead of the number.
3. If the full line does not fit the width, use the short format: two-letter names (`cx`, `cu`, `cl`, and the first two letters for others), percents only, windows joined with `/`, no reset clocks, for example `cx 96/46 · cu 100/25 · cl 58/22`. Only if the short line still does not fit, cut it at the width.
4. The line goes directly under the existing header line; snapshots without `headroom` render exactly as before.

# Acceptance criteria

1. Snapshot with Codex 96%/46% (reset 03:11), Cursor 100%/25%, Claude 58%/22%: width 120 shows the full format with every value; width 60 shows `cx 96/46 · cu 100/25 · cl 58/22` with every value.
2. Same snapshot plus a provider `agy` with two weekly pools 40% and 20%: width 60 still shows every value (`ag 40/20` added).
3. Unknown values render as `?` in both formats.
4. The board uses `headroom_snapshot` (injected fake in tests); a test proves stale data is refreshed through it, not read from the cache file.
5. Snapshots without headroom render exactly as before.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
