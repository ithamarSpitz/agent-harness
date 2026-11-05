---
id: S11-T9
stage: 4
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S11-T1
  - S11-T2
  - S11-T3
  - S11-T4
  - S11-T5
  - S11-T6
  - S11-T7
  - S11-T8
  - S11-T10
allowed_paths:
  - README.md
  - README-HARNESS.md
  - CHANGELOG.md
  - SPEC.md
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Docs for S11 field fixes -->
# Goal

The docs describe the S11 changes for users.

# Decisions (do not reopen)

1. CHANGELOG: a new unreleased section listing each S11 fix in one line (plan-change budget and revalidation, agy usage on Windows, agy effort per model and `agy.effort_models`, reviewer faults reviewed elsewhere, the doctor `.harness/` ignore check, CLIs found after a PATH change, agy exploration and the chosen permission mode, waiting instead of blocking when every provider is temporarily out).
2. README-HARNESS (or README, wherever provider config is documented): document `agy.effort_models`, the reviewer-fault rule, and the agy permission mode from S11-T8 with its reason.
3. SPEC: update only the sections that describe these behaviors.

# Acceptance criteria

1. Every new config key from S11 is documented with its default.

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
