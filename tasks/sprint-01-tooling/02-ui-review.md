---
id: S1-T2
stage: 2
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S1-T1
allowed_paths:
  - harness/**
  - tests/harness/**
  - README.md
context:
  - readme
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

Let the reviewer agent check UI behavior in a real browser through Playwright, when the reviewer itself decides the change can affect the UI. The harness does not decide which tasks need it and does not run any browser itself. It only gives the reviewer the means (how to start the app, the Playwright tools) and cleans up afterwards.

# Decisions (do not reopen)

1. **Config.** A new `ui_review` section, a `UIReviewConfig` dataclass with these defaults: `enabled: false`, `start_commands: []` (shell commands that start the app, run from the worktree root), `url: null` (the page to open), `ports: []` (ports the app listens on), `playwright_mcp_package: "@playwright/mcp@0.0.84"`. When `enabled` is true, `start_commands` and `url` are required: config loading or `harness validate` reports a clear error otherwise. A missing section keeps today's behavior.
2. **Review prompt.** When enabled, `review_prompt` adds this section, with the commands and URL filled in, and nothing when disabled:

   ```text
   UI check (your decision):
   If the changes add or change behavior that can affect what a user sees or does in the UI, check it in a real browser:
   1. Start the app in the background from the repository root: <start commands, one per line>
   2. Open <url> with the Playwright browser tools and try the changed behavior.
   3. Do not change any files.
   If the changes cannot affect the UI, do not start anything.
   After the VERDICT line, add one line: `UI_CHECK: done - <what you tried and what you saw>` or `UI_CHECK: skipped - <why the changes cannot affect the UI>`.
   A UI problem you saw is a FAIL finding.
   ```

   The `VERDICT:` first-line rule stays exactly as it is.
3. **Codex reviewer.** When enabled, the Codex review call:
   - registers the Playwright MCP server with config overrides: `mcp_servers.playwright.command = "npx"` and `mcp_servers.playwright.args = ["-y", "<playwright_mcp_package>"]` (valid TOML values passed through `--config`);
   - runs with the implementation sandbox (the same value `_effective_sandbox(read_only=False)` returns) instead of `read-only`, because the reviewer must be able to start the app.
   When disabled, the review call stays read-only and unchanged.
4. **Cursor reviewer.** The harness does not write MCP config into worktrees. Cursor uses the Playwright server from the user's own Cursor MCP config. If the installed Cursor CLI (`agent --help`) has a flag that approves configured MCP servers without a prompt, add it to the Cursor review call only when enabled. Otherwise leave the call unchanged and say so in the README.
5. **Reviewer must not change files.** When enabled, the harness records the worktree state before the review (`git status --porcelain` plus a hash of `git diff HEAD`). If it differs after the review, the review counts as FAIL with the finding "Reviewer changed files in the worktree", listing the changed paths. Ignored files (caches, `node_modules`) do not count.
6. **Cleanup, always.** After every review when enabled (pass, fail, timeout or error), stop processes the review left running:
   - any process listening on a port in `ports`;
   - any process whose command line contains the task worktree's path.
   Use the existing `stop_process_tree` from `harness/process_utils.py`. Find processes with PowerShell (`Get-NetTCPConnection`, `Get-CimInstance Win32_Process`) on Windows and with `ps` (plus `lsof` when available) on POSIX. Never stop the harness's own process or its parents. Log each stopped process (pid and command line) to the task log.
7. **Doctor.** When enabled: error if `npx` is missing; warning if a port in `ports` is already in use; warning if the user's Cursor MCP config (`~/.cursor/mcp.json`) has no server whose name or args mention `playwright`. When disabled, doctor does not mention Playwright.
8. **README.** A short "Reviewer UI check (Playwright)" section: what the reviewer does and that it decides itself, the config keys, the cleanup, and the Cursor MCP requirement.

# Out of scope

Automatic Playwright tests, screenshots stored by the harness, running a browser from the harness, deciding per task whether a UI check is needed, changes to the implementation prompt, `harness init`.

# Acceptance criteria

1. With no `ui_review` section, review prompts and review commands are byte-identical to before; existing tests pass unmodified.
2. Tests prove: the prompt section appears only when enabled; the Codex review command contains the two MCP overrides and the non-read-only sandbox only when enabled; a fake reviewer that edits a tracked file yields a FAIL review naming that file; a fake reviewer that leaves a background process running (a sleeping Python process whose command line contains the worktree path) has that process stopped after the review, on the current OS; config errors for `enabled: true` without `url` or `start_commands`.
3. Doctor tests cover the enabled checks with fakes.
4. No test starts a real browser, `npx` or network call.
