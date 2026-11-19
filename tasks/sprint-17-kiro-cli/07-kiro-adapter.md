---
id: S17-T7
stage: 3
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S17-T2
  - S17-T4
allowed_paths:
  - harness/adapters/kiro.py
  - tests/harness/test_adapter_kiro.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Kiro adapter -->
# Goal

`harness/adapters/kiro.py` holds `KiroAdapter`, the five adapter parts for Kiro, wrapping `KiroAgentRunner` the way `harness/adapters/copilot.py` wraps Copilot.

# Background

Sprint S17 adds Kiro CLI (`kiro-cli`, version 3.1.0, installed at `%LOCALAPPDATA%/Kiro-Cli/kiro-cli.exe`, on PATH) as the first CLI written the new way after S16 (design doc "Harness Redesign", section "Providers as adapters"). S16 added `harness/adapters/` (`base.py`: `BaseAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault`, `command_version`; `AdapterRegistry` in `__init__.py`) and one adapter per CLI that wraps its runner. The closest existing CLI is GitHub Copilot: `harness/copilot_runner.py` (`CopilotAgentRunner`), `harness/copilot_usage.py`, `harness/adapters/copilot.py`, `CopilotConfig` in `harness/config.py`. Copy their structure; do not refactor them. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S17 tasks exist when it runs.

Facts recorded live on 2026-11-19 (do not re-probe; tests use these as samples):
- Headless call: `kiro-cli chat --no-interactive --output-format stream-json --model <id> --trust-workspace [--trust-all-tools | --trust-tools=fs_read,execute_bash]`, prompt on stdin, cwd = workspace. stdout is JSON Lines; a denied tool also prints a plain non-JSON line `[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.`
- Models (`kiro-cli chat --list-models --format json`): auto, claude-sonnet-4.5, claude-haiku-4.5, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. The models report `"reasoning":{"support":"unavailable"}`, so `--effort` is never passed.
- Plan: KIRO FREE, 50 credits per month. `/usage` costs no credits.

# Decisions (do not reopen)

1. `class KiroAdapter(BaseAdapter)`, `name = "kiro"`, `__init__(self, runner: KiroAgentRunner | None, config: HarnessConfig)`. `command` is `config.kiro.command`.
2. `enabled()` is `config.kiro.enabled and runner is not None`.
3. `find(refresh=False)`: version from `command_version`; `signed_in` from running `[command, "whoami"]` (timeout 15 s): exit 0 and stdout containing `Logged in` gives True, exit non-zero gives False, any error gives None. The `whoami` output contains the account e-mail: it is never stored in `FindResult.detail`, logged or printed; `detail` is only `signed in`, `not signed in` or the exception class name.
4. `implementation_budget()` is `len(config.kiro.implementation_sequence)`. `levels()`: worker = sequence[0], hard_worker = sequence[1] if present else sequence[0], escalation = `escalation_model`.
5. `implement(ctx)` calls `runner.implement(ctx.task, ctx.workspace, ctx.timeout_minutes, ctx.provider_attempt + 1, ctx.previous_failure, env=..., model_class_override=ctx.model_class, code_map_offered=..., on_process_start=...)`; `review`, `plan_change` and `brief_check` pass through like the Copilot adapter. A missing runner raises `RuntimeError("kiro runner is not available")`.

# Acceptance criteria

1. `enabled`, `implementation_budget` and `levels` follow decisions 2 and 4 for default and custom config.
2. `find` with a fake subprocess: signed in, not signed in, and an `OSError`; the fake `whoami` output contains `Email: someone@example.com` and no returned field contains it.
3. Each call passes its arguments to a fake runner unchanged.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes and the recorded samples in this brief.
- Never read, print, log or store tokens, credentials or the signed-in account's e-mail. Never change the user's Kiro, agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Kiro stays off by default (`kiro.enabled: false`); nothing changes for a project that does not enable it.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
