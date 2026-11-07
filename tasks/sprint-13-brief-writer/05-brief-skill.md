---
id: S13-T3
stage: 3
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S13-T2
  - S13-T5
allowed_paths:
  - skills/write-harness-briefs/SKILL.md
  - tests/harness/test_brief_skill.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- A skill that writes harness briefs from a spec -->
# Goal

An agent that follows `skills/write-harness-briefs/SKILL.md` turns a spec into harness sprint briefs that pass `harness validate` and `harness brief-check` the first time.

# Background

Sprint S13 builds a brief writer so later sprints get fewer bad briefs: free checks in `harness validate` (T1, T2), a cheap read-through by a model (T4, T5), and a skill that writes briefs (T3). Design doc approved 2026-11-07.

Spec Kit is set up in this repo (`.specify/`, `.claude/skills/speckit-*`); its specs land in `specs/<NNN-feature>/spec.md` (and `plan.md`). Reference material is in `docs/references/brief-sources/` (see its `SOURCES.md`): Superpowers writing-plans, the Spec Kit tasks template and Task Master's prompts. Read them for ideas and write our own skill; do not copy them whole. Good briefs to imitate: `tasks/sprint-11-field-fixes/14-weekly-cap-wait-time.md` and `tasks/sprint-12-agy-test-isolation/01-no-real-agy-in-tests.md`. The roadmap format is in `tasks/roadmap.yaml`.

# Decisions (do not reopen)

1. `SKILL.md` starts with frontmatter `name: write-harness-briefs` and a one-line `description` saying when to use it; the body is at most about 250 lines.
2. It covers, in this order: inputs (the spec, plan and the code); the brief file format (frontmatter fields exactly as the examples, and the sections Goal, Background with file and approximate line pointers, Decisions (do not reopen), numbered Acceptance criteria, Rules with the stop rule); task sizing (one test cycle per task, at most 6 allowed paths and 6 criteria, split rather than cut scope); ordering (dependencies, and stages for tasks that may run side by side when they touch different files); the roadmap entry; and a checklist of our known mistakes (the ones in S13-T1's codes, plus: a docs task must not hide code gaps, so split code gaps into code tasks; live probes must name a model pool that has quota; a docs task depends on the code tasks it documents).
3. It ends with: run `harness validate <SPRINT>`, fix every error and warning or say why a warning stays; run `harness brief-check <TASK>` for each task and fix the brief until it prints OK.
4. Credit the three sources in one short line each.

# Acceptance criteria

1. `tests/harness/test_brief_skill.py`: the file exists, its frontmatter has `name: write-harness-briefs` and a non-empty `description`, and the body mentions `harness validate`, `harness brief-check`, `allowed_paths`, `allow_protected` and "outside allowed_paths".
2. Every command and file path the skill names exists in this repo (the reviewer checks this against the code).

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
