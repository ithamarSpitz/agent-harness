---
id: S7-T11
stage: 6
model_class: hard_worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S7-T6
  - S7-T8
allowed_paths:
  - docs/spec/review.md
context: []
environment: null
verification:
  - >-
    python -c "import pathlib,sys;t=pathlib.Path('docs/spec/review.md').read_text(encoding='utf-8');need=['## Review','## Reviewer UI check','## Code map'];miss=[h for h in need if h not in t];sys.exit('missing: '+', '.join(miss) if miss else 0)"
---

# Goal

Write `docs/spec/review.md`: review, the reviewer UI check and the code map. It is one part of the harness specification; `SPEC.md` is the index that links all parts.

# Scope

What the reviewer gets, how the verdict is parsed, what travels to the next attempt, review resume after waits. The reviewer UI check (Playwright, ports, cleanup). How the code map is offered, updated and copied.

Required headings (exact): `## Review`, `## Reviewer UI check`, `## Code map`.

# Findings from the earlier review of this part (fix them)

- UI cleanup is snapshot-based: only listeners that appeared on `ui_review.ports` after the pre-review snapshot are stopped, plus processes whose command line references the task worktree. Listeners that existed before are left alone.
- Review resume skips verification when the fingerprint matches, for every review-phase wait, not only quota.

# How to write it

- Start from the matching sections of the reviewed draft `tasks/sprint-07-add-task-and-docs/spec-draft.md` (written in an earlier round). Check every sentence against the code in `harness/` and fix what does not match. When the code and the draft disagree, the code wins.
- Describe what the code does today, not what it should do. Behavior that looks like a bug goes in a final `## Known gaps` section of your file, described as it actually behaves.
- Where Codex and Cursor behave differently, say so explicitly; do not describe one provider's rule as universal.
- Plain, precise English, short sections, tables for statuses, transitions and keys. No history.

# Acceptance criteria

1. The required headings exist (checked by the verification command).
2. Every command, status, config key and default named exists in the code with that name and value.
3. No sentence describes behavior the code does not have.
