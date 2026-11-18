---
id: S15-T7
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/terminal.py
  - harness/board_loop.py
  - harness/board.py
  - tests/harness/test_terminal.py
  - tests/harness/test_board.py
  - tests/harness/test_board_loop.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- The live board redraws in place in Windows Terminal -->
# Goal

The live board (`harness run/resume` without `--no-board`, and `harness watch`) redraws in place in Windows Terminal with Windows PowerShell 5.1: no leftover characters, no repeated prompt lines, local times.

# Background

Sprint S15 fixes what went wrong on 2026-11-18 in the harness's own S14 run and in BookNook's S16 run (harness 0.24.0). Evidence: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` items 20-22, and the S14 logs summarized in each brief. agy is turned off in this repo's `harness.yaml` until the agy tasks here are merged; do not turn it on.

Seen 2026-11-18 in both projects (BookNook item 20 and the harness S14 tab): lines that got shorter kept old characters ("Stage 610", "S14-T814-T11", "RETRYMENTING", "CHECKINGTING"), the `harness>` prompt repeated on every refresh until the screen filled, and event times showed UTC (18:34 at 20:34 local). `Screen.draw` (`harness/terminal.py` around line 82) moves up `self._height` lines and rewrites each line followed by `\n` without clearing the rest of the line, and writes a newline after the last line, so a frame of full terminal height scrolls by one row each time. `compose` (`harness/console_screen.py`) already truncates lines to the width. Event times come from `_format_event` / `_aware` in `harness/board.py` around line 203, which convert to UTC.

# Decisions (do not reopen)

1. `Screen.draw`: start each frame with `\r`, write every line as `\x1b[2K` + line, separate lines with `\n` but write none after the last line, then `\x1b[J`; move up `self._height - 1` lines on the next frame (the cursor stays on the last line). Lines are truncated to the current terminal width before writing.
2. `board_loop` asks for at most terminal height lines, so nothing scrolls.
3. Board event times are shown in local time (`astimezone()` with no argument); stored times stay UTC.
4. No change to the plain `--no-board` output or to `harness status`.

# Acceptance criteria

1. `tests/harness/test_terminal.py`: drawing a 3-line frame, then a frame with a shorter second line, writes `\x1b[2K` before every line, no trailing newline, and the cursor-up count is 2.
2. A frame as tall as the terminal produces no extra newline (count `\n` = lines - 1).
3. `tests/harness/test_board.py`: an event at 18:34 UTC renders with the local hour for a fixed local timezone set in the test (for example via `TZ` with `time.tzset` where available, or by injecting the zone).
4. `tests/harness/test_board_loop.py` existing tests pass.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
