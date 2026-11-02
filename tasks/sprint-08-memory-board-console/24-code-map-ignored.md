---
id: S8-T24
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/code_map.py
  - harness/init_project.py
  - tests/harness/test_code_map_exclude.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- The code map cache never makes a project dirty -->
# Goal

`.harness/code-map/` never shows up as untracked in a project.

# Background (found on BookNook sprint S3-polish, Nov 2026)

Since the code map (v0.14), `.harness/code-map/commit.txt` and `.harness/code-map/source/` appear as untracked in projects whose `.gitignore` predates it. That makes the integration checkout dirty, so `resume`, the stage barrier and `doctor`'s repo-clean check can refuse. `graphify-out/` is already handled through `.git/info/exclude` (`ensure_graphify_out_excluded` in `harness/code_map.py`).

# Decisions (do not reopen)

1. Before building or updating the project code map, add `.harness/code-map/` to the project's `.git/info/exclude` the same way as `graphify-out/` (once, no duplicates).
2. `harness init` writes `.harness/` into the generated `.gitignore` if it is not covered already.

# Acceptance criteria

1. After a code map build in a temporary repository without that `.gitignore` line, `git status --porcelain` is empty.
2. Running it twice adds the line once.
3. `harness init` output `.gitignore` covers `.harness/`.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
