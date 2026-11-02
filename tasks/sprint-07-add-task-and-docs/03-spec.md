---
id: S7-T3
stage: 6
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S7-T6
  - S7-T8
allowed_paths:
  - SPEC.md
context: []
environment: null
verification:
  - >-
    python -c "import pathlib,sys;t=pathlib.Path('SPEC.md').read_text(encoding='utf-8');need=['## Purpose','## Concepts','## Guarantees','docs/spec/lifecycle.md','docs/spec/providers.md','docs/spec/review.md','docs/spec/operations.md','docs/spec/configuration.md'];miss=[h for h in need if h not in t];sys.exit('missing: '+', '.join(miss) if miss else 0)"
---

# Goal

Write `SPEC.md`, the entry point of the harness specification: a short overview plus links to its parts. The detailed parts are written by other tasks in this stage: `docs/spec/lifecycle.md` (task lifecycle, stage barrier), `docs/spec/providers.md` (providers, fallback, quota, capacity), `docs/spec/review.md` (review, reviewer UI check, code map), `docs/spec/operations.md` (release, adding tasks during a run), `docs/spec/configuration.md` (configuration reference, task frontmatter).

# Scope

1. `## Purpose`: what problem the harness solves and its core idea (plan as data, isolated worktrees, mechanical checks plus independent review, atomic stages).
2. `## Concepts`: project, roadmap, sprint, stage, task, brief and its frontmatter, worktree, integration checkout, runtime directory (`.harness/`).
3. `## Parts`: one line per part with its link.
4. `## Guarantees`: the invariants the harness enforces (for example: a stage merges only if all its tasks pass; agents change only `allowed_paths`; a failed release never undoes merged code). Each must be true in the code.
5. Keep it under 120 lines. Do not repeat the details of the parts.

# How to write it

- Start from the Purpose, Concepts and Guarantees sections of the reviewed draft `tasks/sprint-07-add-task-and-docs/spec-draft.md` (written in an earlier round). Check every sentence against the code in `harness/` and fix what does not match. When the code and the draft disagree, the code wins.
- Describe what the code does today, not what it should do. Behavior that looks like a bug goes in a final `## Known gaps` section of your file, described as it actually behaves.
- Where Codex and Cursor behave differently, say so explicitly; do not describe one provider's rule as universal.
- Plain, precise English, short sections, tables for statuses, transitions and keys. No history.

# Acceptance criteria

1. The required headings exist (checked by the verification command).
2. Every command, status, config key and default named exists in the code with that name and value.
3. No sentence describes behavior the code does not have.
