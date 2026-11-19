---
id: S17-T14
stage: 3
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: [S17-T5]
allowed_paths:
  - harness/limits.py
  - tests/harness/test_agy_limits.py
  - tests/harness/test_limits.py
  - tests/harness/test_pace.py
  - tests/harness/test_provider_choice.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Antigravity limits show percent used -->
# Goal

`harness limits` shows Antigravity quota as percent used, like Codex and Claude, so all providers read the same way.

# Background

Sprint S17 adds Kiro CLI (`kiro-cli`, version 3.1.0, installed at `%LOCALAPPDATA%/Kiro-Cli/kiro-cli.exe`, on PATH) as the first CLI written the new way after S16 (design doc "Harness Redesign", section "Providers as adapters"). S16 added `harness/adapters/` (`base.py`: `BaseAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault`, `command_version`; `AdapterRegistry` in `__init__.py`) and one adapter per CLI that wraps its runner. The closest existing CLI is GitHub Copilot: `harness/copilot_runner.py` (`CopilotAgentRunner`), `harness/copilot_usage.py`, `harness/adapters/copilot.py`, `CopilotConfig` in `harness/config.py`. Copy their structure; do not refactor them. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S17 tasks exist when it runs.

Facts recorded live on 2026-11-19 (do not re-probe; tests use these as samples):
- Headless call: `kiro-cli chat --no-interactive --output-format stream-json --model <id> --trust-workspace [--trust-all-tools | --trust-tools=fs_read,execute_bash]`, prompt on stdin, cwd = workspace. stdout is JSON Lines; a denied tool also prints a plain non-JSON line `[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.`
- Models (`kiro-cli chat --list-models --format json`): auto, claude-sonnet-4.5, claude-haiku-4.5, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. The models report `"reasoning":{"support":"unavailable"}`, so `--effort` is never passed.
- Plan: KIRO FREE, 50 credits per month. `/usage` costs no credits.


Today `_antigravity_lines` in `harness/limits.py` prints `  Gemini Models: 0% left, resets ...`, while Codex and Claude print `NN% used`. The user asked why agy says "left". S17-T5 adds the Kiro block to the same file; this task runs after it.

# Decisions (do not reopen)

1. The text line becomes `  <group_name>: NN% used, resets <day time> (<in ...>)`, where used = 100 - remaining_fraction * 100, rounded like today.
2. The JSON payload keeps `remaining_percent` unchanged and adds `used_percent`.
3. No change to how quota is fetched or to routing.

# Acceptance criteria

1. A bucket with remaining_fraction 0 prints `100% used`; 0.25 prints `75% used`.
2. The payload dict returned by `_antigravity_lines` has both `remaining_percent` and `used_percent` for each pool.
3. The unavailable line (`Antigravity: usage unavailable (...)`) is unchanged.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes and the recorded samples in this brief.
- Never read, print, log or store tokens, credentials or the signed-in account's e-mail. Never change the user's Kiro, agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Kiro stays off by default (`kiro.enabled: false`); nothing changes for a project that does not enable it.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
