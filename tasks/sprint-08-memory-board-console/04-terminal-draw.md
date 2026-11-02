---
id: S8-T4
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/terminal.py
  - tests/harness/test_terminal.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Terminal drawing primitives -->
# Goal

Redraw a block of lines in place in a terminal, with colors, on Windows, Linux and macOS.

# Decisions (do not reopen)

1. `harness/terminal.py`:
   - `enable_ansi() -> bool`: on Windows set `ENABLE_VIRTUAL_TERMINAL_PROCESSING` through `ctypes` (`GetStdHandle`, `GetConsoleMode`, `SetConsoleMode`); POSIX returns True; any failure returns False.
   - `supports_unicode(stream) -> bool`: whether the stream encoding can encode `✓←`.
   - `color_for(status) -> str`: ANSI color by status group (running group, passed/done, waiting, retry/blocked); empty string when `NO_COLOR` is set.
   - `Screen(stream)`: `draw(lines)` moves the cursor to the top of the previously drawn block, writes the new lines, clears leftovers (`\x1b[J`); `size()` via `shutil.get_terminal_size`; `close()` leaves the cursor below the block.
2. The stream and environment are injectable; tests use `io.StringIO` and assert the escape sequences.

# Acceptance criteria

1. Two consecutive `draw` calls produce cursor-up by the previous height, the new lines and a clear-to-end.
2. `NO_COLOR` removes all color codes; unknown statuses get no color.
3. `enable_ansi` returns False when the fake console calls fail.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
