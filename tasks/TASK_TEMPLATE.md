---
id: S?-T?
stage: 1
model_class: worker
max_attempts: 2
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - src/example/**
context: []
# Set to docker only for tasks that need the isolated Compose environment.
environment: null
# Later prefer logical aliases, e.g. [architecture.ingestion, architecture.storage]
verification:
  - npm test -- example
---

# Goal

One concrete outcome. A task should be small enough for a fresh agent context.

# Required context

Read only the relevant material, for example:

- `ARCHITECTURE.md` — specific heading(s), not the whole file.
- `docs/assignment.md` — specific requirement(s).
- Relevant existing source files.

# Scope

Implement exactly what belongs to this task.

# Out of scope

List nearby work that must not be pulled into this task.

# Invariants

List architectural or correctness properties that must remain true.

# Acceptance criteria

1. Observable behavior one.
2. Observable behavior two.
3. Failure/edge behavior where relevant.

# Verification

The YAML `verification` commands are authoritative and are executed by the harness, not trusted to the agent's self-report.
