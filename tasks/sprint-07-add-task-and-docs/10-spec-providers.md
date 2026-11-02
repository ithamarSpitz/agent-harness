---
id: S7-T10
stage: 6
model_class: hard_worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S7-T6
  - S7-T8
allowed_paths:
  - docs/spec/providers.md
context: []
environment: null
verification:
  - >-
    python -c "import pathlib,sys;t=pathlib.Path('docs/spec/providers.md').read_text(encoding='utf-8');need=['## Providers and fallback','## Quota and capacity'];miss=[h for h in need if h not in t];sys.exit('missing: '+', '.join(miss) if miss else 0)"
---

# Goal

Write `docs/spec/providers.md`: providers, fallback, quota and capacity. It is one part of the harness specification; `SPEC.md` is the index that links all parts.

# Scope

Codex and Cursor routing, per-task and per-phase fallback, model classes and escalation. Then every waiting status and its cause detection per provider, waits, probes, what resumes automatically, `harness quota` commands.

Required headings (exact): `## Providers and fallback`, `## Quota and capacity`.

# Findings from the earlier review of this part (fix them)

- Codex quota, auth or model-unavailable with Cursor enabled does not wait: Codex is disabled for that task and the same call continues on Cursor. `WAITING_QUOTA` happens only when Cursor reports quota (or Cursor is disabled).
- The command is `harness quota set-reset`, not `set-reset-at`.

# How to write it

- Start from the matching sections of the reviewed draft `tasks/sprint-07-add-task-and-docs/spec-draft.md` (written in an earlier round). Check every sentence against the code in `harness/` and fix what does not match. When the code and the draft disagree, the code wins.
- Describe what the code does today, not what it should do. Behavior that looks like a bug goes in a final `## Known gaps` section of your file, described as it actually behaves.
- Where Codex and Cursor behave differently, say so explicitly; do not describe one provider's rule as universal.
- Plain, precise English, short sections, tables for statuses, transitions and keys. No history.

# Acceptance criteria

1. The required headings exist (checked by the verification command).
2. Every command, status, config key and default named exists in the code with that name and value.
3. No sentence describes behavior the code does not have.
