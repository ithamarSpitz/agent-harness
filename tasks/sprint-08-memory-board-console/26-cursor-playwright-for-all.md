---
id: S8-T26
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/runner.py
  - tests/harness/test_cursor_tools.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Cursor agents check the UI with Playwright -->
# Goal

Cursor calls have the same browser setup as Codex calls after S8-T25, so a task that switches provider can keep checking the UI the same way.

# Background

Agents should check UI changes in a browser, but through the Playwright MCP server, which both Codex and Cursor can use. In BookNook S3-polish (Nov 2026) a Codex implementer used the ChatGPT Chrome extension instead (`chrome@openai-bundled` / `browser@openai-bundled` plugins from the user's `~/.codex/config.toml`; 16 `cua_repl` tool calls). That works only for Codex: when a task falls back to Cursor, the same way of checking is not available, and the harness registers Playwright only for review calls. Cursor reads MCP servers from `~/.cursor/mcp.json` (it contains `playwright`, created for reviewer UI checks); review calls already auto-approve it (`cursor_mcp_auto_approve_argv` in `harness/ui_review.py`).

# Decisions (do not reopen)

1. When `ui_review` is enabled, implementation and retry Cursor calls get the same Playwright setup and auto-approval as review calls.
2. If `~/.cursor/mcp.json` has no `playwright` server, log one clear warning per run naming the file (do not edit the user's file).
3. Find out from the installed Cursor CLI (`agent --help`) whether it has its own built-in browser tool and how to keep agents on Playwright; apply it if possible, otherwise list it in OPEN_ISSUES. State the evidence in your report.

# Acceptance criteria

1. Implementation, retry and review Cursor command lines include the Playwright approval when `ui_review` is enabled, and not when it is disabled (fakes).
2. A missing `playwright` entry produces the warning once.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
