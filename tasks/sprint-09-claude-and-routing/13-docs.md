---
id: S9-T13
stage: 8
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S9-T14
  - S9-T16
  - S9-T17
  - S9-T18
  - S9-T19
  - S9-T20
  - S9-T21
  - S9-T22
  - S9-T23
  - S9-T24
  - S9-T25
  - S9-T26
  - S9-T27
  - S9-T28
  - S9-T29
  - S9-T30
  - S9-T7
  - S9-T9
  - S9-T11
  - S9-T12
allowed_paths:
  - docs/spec/providers.md
  - docs/spec/configuration.md
  - docs/spec/review.md
  - docs/spec/operations.md
  - SPEC.md
  - README.md
  - CHANGELOG.md
context:
  - spec
environment: null
verification:
  - git diff --check
---

<!-- Docs for S9 -->
# Goal

The spec parts, README and changelog describe S9 exactly as implemented.

# Scope

1. `docs/spec/providers.md`: Claude Code as a provider, its ladder, quota and error handling, the headroom snapshot, the choice by urgency, the Claude reserve, the reviewer on another provider, Playwright for Claude.
2. `docs/spec/configuration.md`: the `claude:` section and every new key.
3. `README.md`: enabling Claude in a project, `harness models report` and `harness models import`, the shared log location. Under 250 lines.
4. `CHANGELOG.md`: one S9 entry.
5. `docs/spec/review.md`: the required impact step and `IMPACT:` section (S9-T15), the "code outside the diff that uses what changed" list the harness puts in review prompts and its grep fallback (S9-T16), and that the reviewer is never the implementing provider and never Cursor Auto once another reviewer exists.
6. `docs/spec/operations.md`: `HARNESS_HOME` and the machine-wide files under it (`agents.json` ledger, `outcomes.jsonl`), the per-call fields in usage records (S9-T18), verification records, and that tests never touch the real home folder (S9-T17).
7. `docs/spec/operations.md` also: `harness hold` / `harness release` (S9-T25) and brief reload before each attempt plus the agent-reported block rule (S9-T24).
8. `docs/spec/providers.md` also: Cursor Auto takes first implementation attempts and headroom routing applies in every project (S9-T26).
9. `docs/spec/configuration.md` and `docs/spec/providers.md` also: `cursor.free_only` (S9-T30): Cursor is used only while both Cursor pools are at 100%.
10. `SPEC.md`: only the summary/index lines that point to the changed parts, so it stays consistent with them.

# Acceptance criteria

Every statement matches the code; nothing unrelated changes.
