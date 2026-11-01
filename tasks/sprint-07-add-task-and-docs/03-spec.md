---
id: S7-T3
stage: 3
model_class: hard_worker
timeout_minutes: 60
review: true
allow_protected: false
depends_on:
  - S7-T2
allowed_paths:
  - SPEC.md
context: []
environment: null
verification:
  - >-
    python -c "import pathlib,sys;t=pathlib.Path('SPEC.md').read_text(encoding='utf-8');need=['## Purpose','## Concepts','## Task lifecycle','## Stage barrier','## Providers and fallback','## Quota and capacity','## Review','## Code map','## Reviewer UI check','## Release','## Adding tasks during a run','## Configuration reference','## Guarantees'];miss=[h for h in need if h not in t];sys.exit('missing: '+', '.join(miss) if miss else 0)"
---

# Goal

Write `SPEC.md`: the complete, exact description of what the harness is and how it behaves today. It is the source of truth that people and agents working on the harness rely on. It describes the code as it is, not as it should be.

# How to write it

- **Read the code, not the old README.** The README is a changelog with outdated parts. Every statement in SPEC.md must match the current code in `harness/`; when you state a rule, you must have found it in the code. If the code and README disagree, the code wins. If you find behavior that looks like a bug, describe the actual behavior and list it under a final `## Known gaps` section instead of describing intended behavior.
- Plain, precise English. Short sections. Tables for statuses, transitions and config keys. No marketing, no history (history goes to CHANGELOG.md in another task).

# Required sections (exact headings)

1. `## Purpose`: what problem the harness solves and its core idea (plan as data, isolated worktrees, mechanical checks plus independent review, atomic stages).
2. `## Concepts`: project, roadmap, sprint, stage, task, brief and its frontmatter, worktree, integration checkout, runtime directory (`.harness/`).
3. `## Task lifecycle`: every task status, what moves a task between them (table of transitions), attempts and retries, model escalation, BLOCKED and the recovery commands.
4. `## Stage barrier`: merge, stage checks, atomic rollback, what makes a stage fail.
5. `## Providers and fallback`: Codex and Cursor routing, per-task fallback, model classes.
6. `## Quota and capacity`: waits, probes, what resumes automatically.
7. `## Review`: what the reviewer gets and how the verdict is parsed; what travels to the next attempt.
8. `## Code map`: how the map is offered, updated and copied.
9. `## Reviewer UI check`.
10. `## Release`.
11. `## Adding tasks during a run`.
12. `## Configuration reference`: every `harness.yaml` section and key with default and meaning (from `harness/config.py`), and every task frontmatter key.
13. `## Guarantees`: the invariants the harness enforces (for example: the integration checkout is never dirty; a stage merges only if all its tasks pass; agents change only `allowed_paths`; a failed release never undoes merged code).
14. `## Known gaps` (only if you found any).

# Acceptance criteria

1. All headings above exist (checked by the verification command).
2. Every command, status, config key and default named in SPEC.md exists in the code with that name and value.
3. No section describes behavior the code does not have.
