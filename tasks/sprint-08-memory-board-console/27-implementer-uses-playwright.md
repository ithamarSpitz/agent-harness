---
id: S8-T27
stage: 2
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S8-T17
allowed_paths:
  - harness/prompts.py
  - tests/harness/test_prompts_brief.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Tell the implementer how to check the UI -->
# Goal

The coding agent checks its UI changes with Playwright, the same way the reviewer does.

# Background

Agents should check UI changes in a browser, but through the Playwright MCP server, which both Codex and Cursor can use. In BookNook S3-polish (Nov 2026) a Codex implementer used the ChatGPT Chrome extension instead (`chrome@openai-bundled` / `browser@openai-bundled` plugins from the user's `~/.codex/config.toml`; 16 `cua_repl` tool calls). That works only for Codex: when a task falls back to Cursor, the same way of checking is not available, and the harness registers Playwright only for review calls.

# Decisions (do not reopen)

1. When `ui_review` is enabled, implementation and retry prompts say: to check the UI, use the Playwright tools (not other browser or computer-use tools); start the app with the project's `ui_review.command`, open `ui_review.url`, and stop the app before finishing. The prompt includes that command and URL.
2. When `ui_review` is disabled, prompts say nothing about browsers.
3. Review prompts are unchanged.

# Acceptance criteria

1. With `ui_review` enabled, implementation and retry prompts contain the instruction with the configured command and URL; review prompts are unchanged.
2. With it disabled, no browser instruction appears.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
