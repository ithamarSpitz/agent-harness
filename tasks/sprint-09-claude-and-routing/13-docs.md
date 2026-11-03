---
id: S9-T13
stage: 5
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S9-T14
  - S9-T16
  - S9-T7
  - S9-T9
  - S9-T11
  - S9-T12
allowed_paths:
  - docs/spec/providers.md
  - docs/spec/configuration.md
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

# Acceptance criteria

Every statement matches the code; nothing unrelated changes.
