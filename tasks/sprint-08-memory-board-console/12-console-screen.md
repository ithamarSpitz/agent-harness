---
id: S8-T12
stage: 8
model_class: hard_worker
timeout_minutes: 55
review: true
allow_protected: false
depends_on:
  - S8-T5
  - S8-T9
  - S8-T11
allowed_paths:
  - harness/cli.py
  - harness/board_loop.py
  - harness/console_screen.py
  - tests/harness/test_console_screen.py
  - tests/harness/test_board_loop.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Console under the board -->
# Goal

The run/resume/watch terminal becomes a console: board on top, the last command's answer under it, and a `harness> ` prompt at the bottom.

# Decisions (do not reopen)

1. `harness/console_screen.py`: `compose(board_lines, output_lines, prompt_text, cursor, width, height) -> list[str]`: board first, output truncated to the remaining height, prompt last (pure function).
2. When stdin and stdout are both TTYs (and not `--no-board`), the board loop (S8-T11) also polls `KeyReader` and feeds `LineEditor` (S8-T5); on Enter it runs the command (S8-T7, S8-T9) and shows the output. Redraws never lose typed text.
3. `quit` in `watch` exits. In `run`/`resume` it asks to confirm and then acts like `stop`. Ctrl+C acts like today.
4. The terminal mode is restored on every exit path.

# Acceptance criteria

1. `compose` output is exact for fixed inputs at widths 60/100 and heights 15/30.
2. A fake key stream typing `task S1-T1` + Enter shows that command's output; text typed between redraws is preserved.
3. `quit` and an exception restore the terminal (fake terminal).
4. Non-TTY output is unchanged.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
