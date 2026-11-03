---
id: S9-T11
stage: 4
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S9-T3
  - S9-T10
allowed_paths:
  - harness/claude_runner.py
  - tests/harness/test_claude_tools.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Claude agents check the UI with Playwright -->
# Goal

Claude Code agents use the same Playwright browser setup as Codex and Cursor agents (S8-T25/T26), so a task can switch provider and keep checking the UI the same way.

# Decisions (do not reopen)

1. When `ui_review` is enabled, implementation, retry and review Claude calls get a temporary MCP config file (in the task's log folder) with only the Playwright server (same package as the other providers), passed with `--mcp-config <file> --strict-mcp-config`, so the user's own MCP servers are not loaded.
2. When `ui_review` is disabled, pass an empty MCP config with `--strict-mcp-config`.
3. The temporary file is removed after the call.

# Acceptance criteria

1. Command lines contain the right `--mcp-config` / `--strict-mcp-config` arguments for both cases.
2. The temporary file holds only the Playwright server and is removed afterwards.

# Context

The user has three $20 subscriptions: ChatGPT Plus (Codex: 5-hour and weekly windows), Cursor ($20 monthly; its own models Composer/Grok in one pool, other models in another), and Claude Pro (Claude Code: 5-hour and weekly windows). The user's own Claude Code session controls the harness and shares the same Claude limits, so harness agents must leave Claude capacity for it. Today the harness has two providers (Codex first, Cursor as per-task fallback). S9 adds Claude Code as a third provider and chooses the provider per call from live usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
