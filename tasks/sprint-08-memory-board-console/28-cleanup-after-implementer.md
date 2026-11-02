---
id: S8-T28
stage: 2
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S8-T22
allowed_paths:
  - harness/provider_runner.py
  - tests/harness/test_implementer_cleanup.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Clean up app servers the implementer left running -->
# Goal

An app server or browser the coding agent started for a UI check never keeps running after its call, and never blocks the reviewer's port.

# Background

Agents should check UI changes in a browser, but through the Playwright MCP server, which both Codex and Cursor can use. In BookNook S3-polish (Nov 2026) a Codex implementer used the ChatGPT Chrome extension instead (`chrome@openai-bundled` / `browser@openai-bundled` plugins from the user's `~/.codex/config.toml`; 16 `cua_repl` tool calls). That works only for Codex: when a task falls back to Cursor, the same way of checking is not available, and the harness registers Playwright only for review calls. Review calls already snapshot the `ui_review.ports` listeners before the call and stop only new listeners and processes started from the task worktree afterwards (`cleanup_ui_review_processes` in `harness/ui_review.py`, called from `harness/provider_runner.py`). Implementation calls have no cleanup.

# Decisions (do not reopen)

1. When `ui_review` is enabled, wrap implementation and retry calls with the same snapshot and cleanup the review calls use (same function, same cross-project safety: only listeners that were new during the call and processes from this worktree).
2. Cleanup failure is logged and never fails the task.

# Acceptance criteria

1. A fake implementer that leaves a listener on a `ui_review` port has it stopped after the call; a listener that existed before the call is untouched.
2. With `ui_review` disabled, no cleanup runs.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
