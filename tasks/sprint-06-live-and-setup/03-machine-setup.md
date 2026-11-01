---
id: S6-T3
stage: 1
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/machine_setup.py
  - harness/statusline.py
  - harness/cli.py
  - scripts/install.ps1
  - scripts/install.sh
  - tests/harness/test_machine_setup.py
  - tests/harness/test_statusline.py
context: []
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

A new computer gets everything the harness needs with one script, after the user has installed the base tools and logged in. Today several pieces exist only on the developer's machine, set up by hand.

# What exists today only by hand (to be automated)

- Launchers in `~/.local/bin`: `harness.cmd` / `harness` (sh) running `python -P -m harness %*`, and `usage.cmd` / `usage` running `python -P -m harness limits %*`. `-P` keeps a repo checkout in the current directory from shadowing the installed package.
- A Claude Code status line script `~/.claude/statusline-usage.py` plus `"statusLine": {"type": "command", "command": "python <path>"}` in `~/.claude/settings.json`. It reads Claude Code's JSON on stdin, prints `<model> | 5h N% until <day HH:MM> | week N% until <day HH:MM>`, and saves `{"saved_at": <unix>, "rate_limits": <rate_limits>}` to `~/.claude/usage-cache.json` (the fallback source for `harness limits`).
- `~/.cursor/mcp.json` with `{"mcpServers": {"playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@0.0.84"]}}}`.
- `pip install --user graphifyy==0.9.100`.

# Decisions (do not reopen)

1. **`harness setup-machine`** in `harness/machine_setup.py`, wired into `cli.py` like `init` (no project needed). Every step is idempotent and prints `ok`, `created`, `updated` or `skipped (<reason>)`:
   - **Check tools:** `git`, Python ≥ 3.11, `node`/`npx`, `gh`, `codex`, `agent`. Report each missing tool with an install hint; never install system tools. Also report whether `gh`, `codex` and `agent` are logged in (`gh auth status`, `codex login status`, `agent status`), without trying to log in.
   - **Graphify:** `python -m pip install --user graphifyy==0.9.100` when `graphify --help` fails.
   - **Launchers:** the four files above in `~/.local/bin` (create the directory). On Windows, add `~/.local/bin` to the user `PATH` (registry `HKCU\Environment`, through PowerShell `[Environment]::SetEnvironmentVariable(..., "User")`) when missing. On POSIX, append an `export PATH` line to `~/.profile` when missing. Never touch the system PATH.
   - **Claude status line:** the status-line logic moves into the package as `harness/statusline.py` (`python -P -m harness.statusline`), same behavior as described above. Set `statusLine` in `~/.claude/settings.json` to that command when there is no `statusLine`, or when the existing one points at `statusline-usage.py`. Any other existing status line is left alone and reported as `skipped (custom status line)`. Preserve every other setting; write the JSON back with 2-space indent.
   - **Cursor MCP:** add the `playwright` server to `~/.cursor/mcp.json` when absent; keep every other server; create the file when missing.
2. **Scripts.** `scripts/install.ps1` (Windows) and `scripts/install.sh` (POSIX), run from a clone of the harness repo: `python -m pip install --user --upgrade .` (non-editable), then `python -P -m harness setup-machine`. They stop on the first error.
3. **Home directory** comes from a function tests can point at a temp directory; tests never touch the real home, registry or PATH (fake those calls).

# Acceptance criteria

1. On an empty fake home, `setup-machine` creates the launchers, the status-line setting and the MCP file; a second run reports everything `ok` and changes nothing.
2. Existing `settings.json` keys and other MCP servers survive; a custom status line is not replaced; one pointing at `statusline-usage.py` is replaced.
3. Missing tools and missing logins are reported, not installed; the command still exits 0.
4. `harness.statusline` prints the line and writes the cache from a sample stdin; with no `rate_limits` it prints only the model and writes nothing.
