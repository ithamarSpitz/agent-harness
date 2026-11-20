---
id: S17-T13
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/copilot_usage.py
  - tests/harness/test_copilot_usage.py
  - tests/harness/test_copilot_limits.py
  - tests/harness/test_doctor_recovery.py
  - tests/harness/test_limits.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Copilot quota reads when used up -->
# Goal

`harness limits` and routing read the Copilot quota after it is used up, instead of reporting "usage unavailable (invalid remaining)".

# Background

Sprint S17 adds Kiro CLI (`kiro-cli`, version 3.1.0, installed at `%LOCALAPPDATA%/Kiro-Cli/kiro-cli.exe`, on PATH) as the first CLI written the new way after S16 (design doc "Harness Redesign", section "Providers as adapters"). S16 added `harness/adapters/` (`base.py`: `BaseAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault`, `command_version`; `AdapterRegistry` in `__init__.py`) and one adapter per CLI that wraps its runner. The closest existing CLI is GitHub Copilot: `harness/copilot_runner.py` (`CopilotAgentRunner`), `harness/copilot_usage.py`, `harness/adapters/copilot.py`, `CopilotConfig` in `harness/config.py`. Copy their structure; do not refactor them. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S17 tasks exist when it runs.

Facts recorded live on 2026-11-19 (do not re-probe; tests use these as samples):
- Headless call: `kiro-cli chat --no-interactive --output-format stream-json --model <id> --trust-workspace [--trust-all-tools | --trust-tools=fs_read,execute_bash]`, prompt on stdin, cwd = workspace. stdout is JSON Lines; a denied tool also prints a plain non-JSON line `[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.`
- Models (`kiro-cli chat --list-models --format json`): auto, claude-sonnet-4.5, claude-haiku-4.5, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. The models report `"reasoning":{"support":"unavailable"}`, so `--effort` is never passed.
- Plan: KIRO FREE, 50 credits per month. `/usage` costs no credits.


Found live on 2026-11-19: after all 200 premium requests were used, `gh api copilot_internal/user` returns `quota_snapshots.premium_interactions` = `{"entitlement":200,"overage_count":0,"overage_permitted":false,"percent_remaining":0,"remaining":-1,"unlimited":false}`. `parse_copilot_quota` in `harness/copilot_usage.py` rejects `remaining` below 0 (`_integer`), so `harness limits` prints `Copilot: usage unavailable (invalid remaining)` and routing sees no quota data. It worked while `remaining` was positive.

# Decisions (do not reopen)

1. A negative integer `remaining` is accepted and stored as 0 (used up). `entitlement` still must be a non-negative integer.
2. `percent_remaining` below 0 is likewise clamped to 0; above 100 or non-finite stays an error.
3. Non-integer, boolean or missing `remaining` stays an error.
4. No change to how limits prints a quota with 0 remaining.

# Acceptance criteria

1. The recorded sample above parses to remaining 0, percent_remaining 0, entitlement 200.
2. `harness limits` output for that sample shows the Copilot block with 0 left, not "usage unavailable".
3. Existing positive-remaining samples parse as before; a string `remaining` still raises `CopilotUsageError`.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes and the recorded samples in this brief.
- Never read, print, log or store tokens, credentials or the signed-in account's e-mail. Never change the user's Kiro, agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Kiro stays off by default (`kiro.enabled: false`); nothing changes for a project that does not enable it.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
