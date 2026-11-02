---
id: S8-T25
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/codex_runner.py
  - tests/harness/test_codex_plugins.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Codex agents check the UI with Playwright -->
# Goal

Every harness Codex call that may need a browser uses the Playwright MCP server, not the Chrome extension plugins.

# Background

Agents should check UI changes in a browser, but through the Playwright MCP server, which both Codex and Cursor can use. In BookNook S3-polish (Nov 2026) a Codex implementer used the ChatGPT Chrome extension instead (`chrome@openai-bundled` / `browser@openai-bundled` plugins from the user's `~/.codex/config.toml`; 16 `cua_repl` tool calls). That works only for Codex: when a task falls back to Cursor, the same way of checking is not available, and the harness registers Playwright only for review calls.

# Decisions (do not reopen)

1. Register the Playwright MCP server (same `--config mcp_servers.playwright...` overrides review calls use today) on implementation and retry calls too, whenever `ui_review` is enabled for the project.
2. On every harness Codex call, disable the browser and computer-use plugins with `--config` overrides setting `enabled = false` for: `chrome@openai-bundled`, `browser@openai-bundled`, `computer-use@openai-bundled`, `unified-computer-use@openai-bundled`, plus any other plugin id in the user's config whose id contains `chrome`, `browser` or `computer-use`. Other plugins are untouched.
3. Confirm the override syntax for quoted plugin ids against the installed Codex CLI (`codex exec --help`, or a dry run that prints the effective config) and state the evidence in your report.

# Acceptance criteria

1. Implementation, retry and review command lines contain the Playwright overrides when `ui_review` is enabled, and none of them when it is disabled.
2. With a fake config listing chrome, browser and slack plugins, chrome and browser are disabled and slack is not.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
