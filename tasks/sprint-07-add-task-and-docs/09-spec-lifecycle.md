---
id: S7-T9
stage: 6
model_class: hard_worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S7-T6
  - S7-T8
allowed_paths:
  - docs/spec/lifecycle.md
context: []
environment: null
verification:
  - >-
    python -c "import pathlib,sys;t=pathlib.Path('docs/spec/lifecycle.md').read_text(encoding='utf-8');need=['## Task lifecycle','## Stage barrier'];miss=[h for h in need if h not in t];sys.exit('missing: '+', '.join(miss) if miss else 0)"
---

# Goal

Write `docs/spec/lifecycle.md`: the task lifecycle and the stage barrier. It is one part of the harness specification; `SPEC.md` is the index that links all parts.

# Scope

Every task status, what moves a task between them (a transitions table), attempts and retries, model escalation, stalls and timeouts, BLOCKED and every recovery command with exactly what it resets. Then the stage barrier: merge, stage checks, atomic rollback, what makes a stage fail, what happens to waiting tasks.

Required headings (exact): `## Task lifecycle`, `## Stage barrier`.

# Findings from the earlier review of this part (fix them)

- Review-phase waits (`waiting_phase == "review"`, any `WAITING_*`, including `WAITING_TIMEOUT` after a reviewer stall) resume through verification fingerprint check and review, never `IMPLEMENTING`.
- `harness retry` and `harness unblock` set `RETRY` and `attempt = 0` only; they do not reset `provider_attempts` or `stall_counts` (a real fresh budget is `rerun-blocker` / `run-task --fresh-budget`). Describe it as it is and list it under Known gaps.
- The stall watchdog differs: Cursor counts CLI output and worktree file activity; Codex counts only CLI output.
- A stage does not start merging while any of its tasks is waiting on quota or capacity.

# How to write it

- Start from the matching sections of the reviewed draft `tasks/sprint-07-add-task-and-docs/spec-draft.md` (written in an earlier round). Check every sentence against the code in `harness/` and fix what does not match. When the code and the draft disagree, the code wins.
- Describe what the code does today, not what it should do. Behavior that looks like a bug goes in a final `## Known gaps` section of your file, described as it actually behaves.
- Where Codex and Cursor behave differently, say so explicitly; do not describe one provider's rule as universal.
- Plain, precise English, short sections, tables for statuses, transitions and keys. No history.

# Acceptance criteria

1. The required headings exist (checked by the verification command).
2. Every command, status, config key and default named exists in the code with that name and value.
3. No sentence describes behavior the code does not have.
