---
id: S9-T22
stage: 3
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on:
  - S9-T16
allowed_paths:
  - harness/impact.py
  - tests/harness/test_impact.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- The callers list from the grep fallback shows real Python callers -->

# Goal

When the code map is missing or unreadable, the "Code outside the diff that uses what changed" list (S9-T16, `harness/impact.py`) shows real Python callers, not documentation lines.

# Background

On Nov 3 2026 `collect_outside_impact` run on the S9-T16 worktree (code map unreadable) listed 1353 lines; the first 40 were all Markdown (CHANGELOG.md, README.md, HARNESS-OPERATOR.md) matching common changed names like `run` and `write`. A reviewer gets nothing useful from that.

# Decisions (do not reopen)

1. The grep fallback searches only `*.py` files and skips the files changed in the diff.
2. At most 5 users per changed symbol, so one common name cannot fill the list; the overall 40-line cap and "and N more" stay.
3. Users under `harness/` (the package) come before `tests/`; within each, sorted by path and line.
4. When the map could not be read, the list starts with the line `code map unreadable, used grep`; when there is no map, `no code map, used grep`. The logged warning from S9-T16 stays.
5. The graph path is unchanged.

# Acceptance criteria

1. A fixture repo where a README, a changed file and two Python files mention a changed symbol: only the two Python files are listed, the `harness/` one first.
2. A symbol used in 8 places lists 5.
3. Broken and missing maps give their header lines.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
