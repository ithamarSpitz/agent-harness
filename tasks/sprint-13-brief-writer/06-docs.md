---
id: S13-T6
stage: 4
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S13-T3
allowed_paths:
  - README.md
  - CHANGELOG.md
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Docs: brief checks, brief-check, the brief skill, Spec Kit -->
# Goal

The README and CHANGELOG describe what S13 added, matching the merged code.

# Background

Sprint S13 builds a brief writer so later sprints get fewer bad briefs: free checks in `harness validate` (T1, T2), a cheap read-through by a model (T4, T5), and a skill that writes briefs (T3). Design doc approved 2026-11-07.

# Decisions (do not reopen)

1. README: a short "Writing briefs" section: Spec Kit for the spec (`.specify/`, `specs/`), `skills/write-harness-briefs/SKILL.md`, the brief findings `harness validate <SPRINT>` prints (list the codes from `harness/brief_lint.py`), and `harness brief-check <TASK>` (Codex, read-only, exit codes).
2. CHANGELOG: an "Unreleased" entry with the same points in a few lines.
3. Describe only what the code does; if code and this brief disagree, follow the code and say so in your report.

# Acceptance criteria

1. Every command, flag, code and path named in the new text exists in the code.
2. Nothing outside these two files changes.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
