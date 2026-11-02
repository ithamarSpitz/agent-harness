---
id: S8-T17
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/prompts.py
  - tests/harness/test_prompts_brief.py
  - tests/harness/test_code_map.py
  - tests/harness/test_ui_review.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Agents always get the current brief -->
# Goal

The coding agent and the reviewer always work from the current brief in the plan, never from an older copy inside their task worktree.

# Background

`harness/prompts.py` tells agents to "read the task brief at <path>". They read it inside their worktree, which is a checkout of the commit the task started from. When the plan changes later (a task is split, review findings are added to its brief, an added task's brief is edited), the agent and reviewer still read the old copy. In S7 (Nov 2026) an agent repeated the same mistake because the finding added to its brief was invisible to it.

# Decisions (do not reopen)

1. Every prompt that names the brief (implementation, retry, review, and any other prompt in `prompts.py`) embeds the full current brief text, read from the project's integration checkout (the path the roadmap points to), under a heading `Task brief (current version)`.
2. The prompt says: this embedded brief is the current version; if the copy in your worktree differs, follow the embedded one and do not edit the worktree copy.
3. If the brief cannot be read, the prompt falls back to today's path instruction and says so.

# Acceptance criteria

1. With a brief changed after the worktree was created (fake files), implementation, retry and review prompts contain the new text and the instruction.
2. An unreadable brief produces the fallback text.
3. Existing prompt tests pass (update expected text only where the brief embedding is added).

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
