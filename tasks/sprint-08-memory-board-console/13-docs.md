---
id: S8-T13
stage: 9
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S8-T12
  - S8-T15
  - S8-T17
  - S8-T18
  - S8-T19
  - S8-T22
  - S8-T23
  - S8-T24
  - S8-T27
  - S8-T28
  - S8-T29
  - S8-T30
  - S8-T32
  - S8-T39
  - S8-T41
  - S8-T42
  - S8-T43
  - S8-T44
allowed_paths:
  - SPEC.md
  - README.md
  - CHANGELOG.md
context:
  - spec
environment: null
verification:
  - git diff --check
---

<!-- Docs for S8 -->
# Goal

`SPEC.md`, `README.md` and `CHANGELOG.md` describe S8 exactly as implemented.

# Scope

1. `SPEC.md`: retries inside a stage (S8-T16), the current brief in prompts (S8-T17), quota waits until the reset (S8-T18), version bump at release (S8-T19), continuing on new code when the harness builds itself (S8-T20), launch failures (S8-T14), worktree safety (S8-T21 to S8-T23), the code map cache ignored by Git (S8-T24), agents checking the UI with Playwright on both providers and cleanup after them (S8-T25 to S8-T28), start rule by free memory, the machine-wide agent ledger and fair sharing of memory between projects (S8-T33, S8-T34), resource launch failures, the Cursor stream-json verdict fix (S8-T35), absolute brief paths (S8-T36), Cursor's out-of-usage message (S8-T37), the verification time limit (S8-T38), the learned estimate, the fallback, new config keys (configuration reference), the event feed, the stop request, the board, `harness watch`, `--no-board`, the console and every console command.
2. `README.md`: a short section on the board and console with the command list; keep it under 250 lines.
3. `CHANGELOG.md`: one S8 entry.

# Acceptance criteria

Every statement matches the code in `harness/`; nothing unrelated changes.
