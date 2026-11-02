---
id: S7-T14
stage: 7
model_class: hard_worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S7-T10
allowed_paths:
  - docs/spec/providers.md
context: []
environment: null
verification:
  - >-
    python -c "import pathlib,sys;t=pathlib.Path('docs/spec/providers.md').read_text(encoding='utf-8');need=['## Providers and fallback','## Quota and capacity','## Known gaps'];miss=[h for h in need if h not in t];sys.exit('missing: '+', '.join(miss) if miss else 0)"
---

# Goal

Check and correct the `## Quota and capacity` section and the `## Known gaps` section of `docs/spec/providers.md`. The `## Providers and fallback` section was finished and reviewed by S7-T10: do not change it.

# Scope

Every waiting status (`WAITING_QUOTA`, `WAITING_NETWORK`, `WAITING_PROVIDER_BUSY`, `WAITING_PROVIDER_ERROR`, `WAITING_TIMEOUT`) and how its cause is detected per provider, failure records and exit codes, capacity policies, sleeps and probes, what resumes automatically after each wait, the `harness quota` commands (the command is `harness quota set-reset`), and the default pattern lists in `harness/config.py`.

# Known facts to keep correct

- Codex quota, auth or model-unavailable with Cursor enabled does not wait: Codex is disabled for that task and the same call continues on Cursor. `WAITING_QUOTA` happens only when Cursor reports quota (or Cursor is disabled).
- A stall (timeout) is different: the task pauses with `WAITING_TIMEOUT` and another provider is used only on the next `run`/`resume` (see the Providers section).

# How to write it

- Check every sentence against the code in `harness/`. When the code and the text disagree, the code wins.
- Describe what the code does today. Behavior that looks like a bug goes in the `## Known gaps` section at the end, described as it actually behaves.
- Where Codex and Cursor behave differently, say so explicitly.
- Plain, precise English, short sections, tables where useful. No history.

# Acceptance criteria

1. The required headings exist (checked by the verification command).
2. Every command, status, config key and default named exists in the code with that name and value.
3. No sentence in your section describes behavior the code does not have.
