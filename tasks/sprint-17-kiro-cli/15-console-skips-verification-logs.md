---
id: S17-T15
stage: 5
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/console_commands.py
  - tests/harness/test_console_commands.py
  - tests/harness/test_verifier.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Console agent-log lookup skips verification logs -->
# Goal

The console's "latest agent log" never returns a verification log, so the S17-T12 test passes every time instead of depending on file timestamps.

# Background

Sprint S17 adds Kiro CLI (`kiro-cli`, version 3.1.0, installed at `%LOCALAPPDATA%/Kiro-Cli/kiro-cli.exe`, on PATH) as the first CLI written the new way after S16 (design doc "Harness Redesign", section "Providers as adapters"). S16 added `harness/adapters/` (`base.py`: `BaseAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault`, `command_version`; `AdapterRegistry` in `__init__.py`) and one adapter per CLI that wraps its runner. The closest existing CLI is GitHub Copilot: `harness/copilot_runner.py` (`CopilotAgentRunner`), `harness/copilot_usage.py`, `harness/adapters/copilot.py`, `CopilotConfig` in `harness/config.py`. Copy their structure; do not refactor them. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S17 tasks exist when it runs.

Facts recorded live on 2026-11-19 (do not re-probe; tests use these as samples):
- Headless call: `kiro-cli chat --no-interactive --output-format stream-json --model <id> --trust-workspace [--trust-all-tools | --trust-tools=fs_read,execute_bash]`, prompt on stdin, cwd = workspace. stdout is JSON Lines; a denied tool also prints a plain non-JSON line `[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.`
- Models (`kiro-cli chat --list-models --format json`): auto, claude-sonnet-4.5, claude-haiku-4.5, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. The models report `"reasoning":{"support":"unavailable"}`, so `--effort` is never passed.
- Plan: KIRO FREE, 50 credits per month. `/usage` costs no credits.



Found at the S17 stage 5 integration (2026-11-20): S17-T12 made `Verifier.verify_task` also write `{task.id}-attempt-{n}-verification.log`. `_agent_log_paths` in `harness/console_commands.py` globs `{task_id}-attempt-*.log`, which now also matches those verification logs, so `_latest_agent_log` can return `T-attempt-1-verification.log`. `tests/harness/test_verifier.py::test_verify_task_attempt_logs_and_latest` failed at the stage gate with: `assert _latest_agent_log(context, "T") == agent_log` got `.../T-attempt-1-verification.log`, expected `.../T-attempt-2.log`. It passed in stage 1 only because the files had equal timestamps.

# Decisions (do not reopen)

1. `_agent_log_paths` drops any path whose name ends with `-verification.log`. The other patterns stay as they are.
2. Among the remaining agent logs, the newest by mtime still wins; when mtimes are equal, the higher attempt number wins (parse `-attempt-<n>` from the name; names without a number sort lowest).
3. No change to `harness/verifier.py` or the log file names.

# Acceptance criteria

1. With `T-attempt-1-verification.log`, `T-attempt-2-verification.log` and `T-attempt-2.log` present, `_latest_agent_log` returns `T-attempt-2.log`, even when the verification logs are newer.
2. With `T-attempt-1.log` and `T-attempt-2.log` given the same mtime (set with `os.utime`), it returns `T-attempt-2.log`.
3. `test_verify_task_attempt_logs_and_latest` passes 20 times in a row (`pytest --count` is not available; a loop inside a new test or repeated runs is fine).

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes and the recorded samples in this brief.
- Never read, print, log or store tokens, credentials or the signed-in account's e-mail. Never change the user's Kiro, agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Kiro stays off by default (`kiro.enabled: false`); nothing changes for a project that does not enable it.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
