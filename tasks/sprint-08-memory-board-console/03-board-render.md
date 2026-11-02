---
id: S8-T3
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/board.py
  - tests/harness/test_board.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Board rendering as a pure function -->
# Goal

The text of the live board, produced by a pure function, so later tasks only have to draw it.

# Decisions (do not reopen)

1. `harness/board.py`:
   - `BoardSnapshot` dataclass: sprint id and title, current stage, number of stages, optional parallel line values (running, max, free GB, per-agent GB), stages (number, merged flag, tasks), tasks (id, status, provider, model, attempt, max attempts, phase elapsed seconds, dependency ids, added flag), events (time, text).
   - `render_board(snapshot, width, *, unicode=True) -> list[str]`:
     - header line: `<sprint>   stage <n>/<total>` plus `   parallel <r>/<max> (free <x> GB, ~<y> GB/agent)` when present;
     - per stage: `Stage N` (+ `  ✓ merged`), then one row per task: id, status, `provider model` while an agent runs, `try n/max`, elapsed as `12m` / `1h05m`, `← dep1, dep2`, `(added)` for added tasks;
     - a blank line and the last 5 events newest first as `HH:MM text`.
   - Every line is cut to `width` (never wraps). With `unicode=False` use `ok merged` and `<-`.
   - `snapshot_from_state(state, sprint, now) -> BoardSnapshot`: builds the snapshot from the harness state objects and the roadmap sprint (pure; events passed in as a list).
2. No I/O, no terminal code, no colors in this module.

# Acceptance criteria

1. Fixed snapshots (several stages, dependencies, running agent, waiting task, blocked task, added task, merged stage) render to exact expected lines.
2. Every line fits widths 40, 80 and 120; `unicode=False` contains only ASCII.
3. `snapshot_from_state` maps a fake state and sprint correctly.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
