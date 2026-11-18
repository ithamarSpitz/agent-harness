---
id: S15-T9
stage: 4
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S15-T1
  - S15-T2
  - S15-T3
  - S15-T4
  - S15-T5
  - S15-T6
  - S15-T7
  - S15-T8
allowed_paths:
  - README.md
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

<!-- Docs for the S15 fixes -->
# Goal

README, CHANGELOG and SPEC describe the S15 behavior as merged.

# Background

Sprint S15 fixes what went wrong on 2026-11-18 in the harness's own S14 run and in BookNook's S16 run (harness 0.24.0). Evidence: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` items 20-22, and the S14 logs summarized in each brief. agy is turned off in this repo's `harness.yaml` until the agy tasks here are merged; do not turn it on.

# Decisions (do not reopen)

1. CHANGELOG: an "Unreleased" entry, one line per merged S15 task.
2. README: Copilot defaults and the auto/effort rule; agy implementation permissions; what counts as an attempt; protected-file failures block at once.
3. SPEC.md: update only sentences that the merged code now contradicts (attempt counting, protected failures, agy and Copilot fault handling).
4. Describe only what the code does; if code and this brief disagree, follow the code and report it. Code gaps are not fixed here; report them.

# Acceptance criteria

1. Every command, flag, setting and path named in the new text exists in the code.
2. Nothing outside these three files changes.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
