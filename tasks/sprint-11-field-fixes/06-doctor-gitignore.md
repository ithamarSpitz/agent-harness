---
id: S11-T6
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/doctor.py
  - tests/harness/test_doctor_gitignore.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- doctor warns when .harness/ files are not ignored (report item 10) -->
# Goal

`harness doctor` tells the user when the project does not ignore the harness's own files, before a stage refuses to start on a "dirty" checkout.

# Background

BookNook's `.gitignore` listed `.harness` files one by one, so 0.19's new `.harness/scheduler-owner` showed up as untracked and `resume` refused to start a stage. New projects are fine (`harness init` writes `.harness/`, see `GITIGNORE_LINES` in `harness/init_project.py`); older ones are not.

# Decisions (do not reopen)

1. New doctor check `repo/harness-ignored`: run `git check-ignore -q .harness/scheduler-owner` (a path inside `.harness/` that need not exist) from the project root. Ignored: OK. Not ignored: WARN with the fix "add `.harness/` to .gitignore (or .git/info/exclude)". Not a git repo: SKIP.
2. Doctor never edits `.gitignore` itself.

# Acceptance criteria

1. New test `tests/harness/test_doctor_gitignore.py` with temp git repos: `.harness/` ignored gives OK; only `.harness/state.json` listed gives WARN with the fix text; no git gives SKIP.

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
