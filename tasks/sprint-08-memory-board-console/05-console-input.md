---
id: S8-T5
stage: 1
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/console_input.py
  - tests/harness/test_console_input.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Non-blocking line input -->
# Goal

Read a typed command line key by key without blocking, so the board can keep redrawing while the user types.

# Decisions (do not reopen)

1. `harness/console_input.py`:
   - `LineEditor`: pure state machine. `feed(key) -> str | None` returns a full line on Enter. Supports printable characters, Backspace, Left/Right, Home/End, Up/Down command history (last 50), Ctrl+C (raises `KeyboardInterrupt`), Escape (clears the line). `text` and `cursor` are readable for drawing.
   - `KeyReader`: `poll() -> list[str]` returns keys typed since the last call, never blocking. Windows: `msvcrt.kbhit` / `msvcrt.getwch` (map the `\xe0`/`\x00` prefixes to arrow names). POSIX: `termios` + `tty.setcbreak` + `select` with timeout 0; map escape sequences to arrow names.
   - `KeyReader` is a context manager that always restores the terminal mode (on exit, exception and `KeyboardInterrupt`).
2. Platform functions are injectable; tests use fakes.

# Acceptance criteria

1. `LineEditor` handles editing, history and Escape exactly (table-driven tests).
2. Fake Windows and POSIX key streams map to the same key names.
3. The terminal mode is restored after normal exit, an exception and Ctrl+C (fake termios records calls).

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
