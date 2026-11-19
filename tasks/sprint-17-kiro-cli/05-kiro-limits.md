---
id: S17-T5
stage: 2
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S17-T1
  - S17-T2
allowed_paths:
  - harness/limits.py
  - tests/harness/test_kiro_limits.py
  - tests/harness/test_limits.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- harness limits shows Kiro -->
# Goal

`harness limits` shows a Kiro block with credits used, limit and reset date when Kiro is enabled, and an off line when it is not, like Antigravity and Copilot after S16-T17.

# Background

Sprint S17 adds Kiro CLI (`kiro-cli`, version 3.1.0, installed at `%LOCALAPPDATA%/Kiro-Cli/kiro-cli.exe`, on PATH) as the first CLI written the new way after S16 (design doc "Harness Redesign", section "Providers as adapters"). S16 added `harness/adapters/` (`base.py`: `BaseAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault`, `command_version`; `AdapterRegistry` in `__init__.py`) and one adapter per CLI that wraps its runner. The closest existing CLI is GitHub Copilot: `harness/copilot_runner.py` (`CopilotAgentRunner`), `harness/copilot_usage.py`, `harness/adapters/copilot.py`, `CopilotConfig` in `harness/config.py`. Copy their structure; do not refactor them. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S17 tasks exist when it runs.

Facts recorded live on 2026-11-19 (do not re-probe; tests use these as samples):
- Headless call: `kiro-cli chat --no-interactive --output-format stream-json --model <id> --trust-workspace [--trust-all-tools | --trust-tools=fs_read,execute_bash]`, prompt on stdin, cwd = workspace. stdout is JSON Lines; a denied tool also prints a plain non-JSON line `[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.`
- Models (`kiro-cli chat --list-models --format json`): auto, claude-sonnet-4.5, claude-haiku-4.5, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. The models report `"reasoning":{"support":"unavailable"}`, so `--effort` is never passed.
- Plan: KIRO FREE, 50 credits per month. `/usage` costs no credits.

# Decisions (do not reopen)

1. `_kiro_settings(config=None, *, root=None, config_path=None) -> tuple[bool, list[str] | None]` in `limits.py`, same file and machine-config rules as `_copilot_settings` (after S16-T17).
2. `load_kiro(command, source=fetch_kiro_usage, now) -> tuple[list[str], dict]`. Text: header `Kiro (<plan in lower case without the KIRO prefix>)`, e.g. `Kiro (free)`, then `  credits  <used:.2f> of <limit:g> used (<used_percent:.1f>%), resets <day and date> (<n> days left)`, using the same date helpers as the Copilot block. JSON: `{"plan", "used", "limit", "used_percent", "resets_at"}` (ISO). A `KiroUsageError` gives `  unavailable: <reason>` and JSON `{"error": <reason>}`.
3. `render_limits` and `run_limits` take `kiro_enabled`, `kiro_quota_command`, `kiro_source` like Copilot. The Kiro block comes last. When off, the text has `Kiro  off (set kiro.enabled: true in harness.yaml or <machine config path>)`; JSON has no `kiro` key.

# Acceptance criteria

1. Enabled with a fake source: text has `Kiro (free)` and `0.20 of 50 used (0.4%)`; JSON has the `kiro` key.
2. A source raising `KiroUsageError("not logged in")` shows `unavailable: not logged in`.
3. Off: the off line is shown and the source is not called.
4. Existing limits output for the other providers is unchanged.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes and the recorded samples in this brief.
- Never read, print, log or store tokens, credentials or the signed-in account's e-mail. Never change the user's Kiro, agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Kiro stays off by default (`kiro.enabled: false`); nothing changes for a project that does not enable it.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
