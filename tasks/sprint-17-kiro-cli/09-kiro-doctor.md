---
id: S17-T9
stage: 5
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S17-T8
allowed_paths:
  - harness/cli.py
  - tests/harness/test_doctor_kiro.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- harness doctor checks Kiro -->
# Goal

`harness doctor` checks Kiro's version, sign-in and credits when Kiro is enabled, like it does for Copilot.

# Background

Sprint S17 adds Kiro CLI (`kiro-cli`, version 3.1.0, installed at `%LOCALAPPDATA%/Kiro-Cli/kiro-cli.exe`, on PATH) as the first CLI written the new way after S16 (design doc "Harness Redesign", section "Providers as adapters"). S16 added `harness/adapters/` (`base.py`: `BaseAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault`, `command_version`; `AdapterRegistry` in `__init__.py`) and one adapter per CLI that wraps its runner. The closest existing CLI is GitHub Copilot: `harness/copilot_runner.py` (`CopilotAgentRunner`), `harness/copilot_usage.py`, `harness/adapters/copilot.py`, `CopilotConfig` in `harness/config.py`. Copy their structure; do not refactor them. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S17 tasks exist when it runs.

Facts recorded live on 2026-11-19 (do not re-probe; tests use these as samples):
- Headless call: `kiro-cli chat --no-interactive --output-format stream-json --model <id> --trust-workspace [--trust-all-tools | --trust-tools=fs_read,execute_bash]`, prompt on stdin, cwd = workspace. stdout is JSON Lines; a denied tool also prints a plain non-JSON line `[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.`
- Models (`kiro-cli chat --list-models --format json`): auto, claude-sonnet-4.5, claude-haiku-4.5, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. The models report `"reasoning":{"support":"unavailable"}`, so `--effort` is never passed.
- Plan: KIRO FREE, 50 credits per month. `/usage` costs no credits.

# Decisions (do not reopen)

1. Three checks when `config.kiro.enabled`: `kiro-cli/version` (like `copilot-cli/version`), `kiro-cli/login` (from `KiroAdapter.find().signed_in`: True passes with `signed in`, False fails with `not signed in: run kiro-cli login`, None fails with `unknown`), `kiro-quota` (`<left:.2f> of <limit:g> credits left, resets <date>`, or `unavailable: <reason>`).
2. `required` like Copilot: required only when no other provider is enabled.
3. The account e-mail never appears in doctor output.

# Acceptance criteria

1. Enabled with fakes: three passing checks with the decision 1 text.
2. Not signed in: `kiro-cli/login` fails with the hint.
3. Disabled: no Kiro checks.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes and the recorded samples in this brief.
- Never read, print, log or store tokens, credentials or the signed-in account's e-mail. Never change the user's Kiro, agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Kiro stays off by default (`kiro.enabled: false`); nothing changes for a project that does not enable it.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
