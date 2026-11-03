---
id: S9-T15
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/prompts.py
  - tests/harness/test_review_prompt_impact.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Reviewers check what a change breaks outside the diff -->

# Goal

Every review checks the effect of the change on the rest of the code, not only the diff against the brief.

# Background

In S8 (Nov 2026) Cursor Auto reviewed 48 tasks. Its FAILs were all correct, but 5 of its PASSes needed follow-up tasks because the change broke or left something outside the diff (S8-T42, T45, T46, T48, S9-T14). The review prompt (`review_prompt` in `harness/prompts.py`) only lists diff-local checks, and offers the code map as optional ("You decide whether it helps"). The usage log shows reviewers used the code map in 0 of 155 review calls.

# Decisions (do not reopen)

1. Add a required step to the review prompt, before the verdict: list every changed function, class, config key, CLI command or output format; for each, find its users outside the diff (with the code map when offered: `graphify query` for callers and importers; otherwise `git grep`), and check they still behave correctly with the change.
2. When the code map is offered, the review prompt tells the reviewer to use it for that step (not "you decide"). Implementation prompts keep today's optional wording.
3. Add "effects on code outside the diff (callers, importers, config and CLI users)" to the "Check specifically for" list.
4. After the verdict line, the reviewer writes a section starting with the line `IMPACT:` listing each changed item and the users it checked (or `none outside the diff`). The verdict line stays the first non-empty line, so verdict parsing is unchanged.

# Acceptance criteria

1. The review prompt contains the impact step, the new check item and the `IMPACT:` instruction, with and without the code map; with it, the graphify instruction is mandatory wording.
2. Implementation and retry prompts are unchanged.
3. Existing prompt tests pass.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
