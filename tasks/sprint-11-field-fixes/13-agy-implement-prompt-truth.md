---
id: S11-T13
stage: 4
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/agy_runner.py
  - tests/harness/test_agy_runner.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- agy implementation prompt must not claim diff context it does not have -->
# Goal

The extra guidance agy gets is true for each call: implementation prompts do not say a base commit, changed files or diff summary are present.

# Background

Found by the S11-T9 docs review on 2026-11-06 (two failed reviews): the docs describe this behavior, the merged code does not do it. A docs-only task cannot fix code, so this task fixes the code and S11-T9 documents it afterwards.

`harness/agy_runner.py` defines one `_EXPLORATION_GUIDANCE` string (around line 73) that says "The base commit, changed files, and diff summary are already in the prompt", and appends it to both the implementation prompt (around line 695) and the review prompt (around line 741). Only the review prompt has that context. A test in `tests/harness/test_agy_runner.py` asserts the wrong text for implementation.

# Decisions (do not reopen)

1. Split it into two constants. Implementation: use agy's own file tools to list, read and search files instead of shell commands; do not run `graphify`. Review: the same, plus the sentence about the base commit, changed files and diff summary.
2. Keep the exact read-only git forms and anything else S11-T8 added; change only the false sentence.

# Acceptance criteria

1. Test: the implementation prompt contains the file-tools guidance and does not contain "diff summary".
2. Test: the review prompt contains both.
3. Update the existing test that asserted the old implementation text.

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
