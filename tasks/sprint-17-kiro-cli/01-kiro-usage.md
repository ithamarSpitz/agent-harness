---
id: S17-T1
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/kiro_usage.py
  - tests/harness/test_kiro_usage.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Read Kiro credits and reset date from /usage -->
# Goal

A new module `harness/kiro_usage.py` reads Kiro's monthly credits (used, limit, reset date, plan) from `kiro-cli chat --no-interactive /usage`, like `harness/copilot_usage.py` does for Copilot.

# Background

Sprint S17 adds Kiro CLI (`kiro-cli`, version 3.1.0, installed at `%LOCALAPPDATA%/Kiro-Cli/kiro-cli.exe`, on PATH) as the first CLI written the new way after S16 (design doc "Harness Redesign", section "Providers as adapters"). S16 added `harness/adapters/` (`base.py`: `BaseAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault`, `command_version`; `AdapterRegistry` in `__init__.py`) and one adapter per CLI that wraps its runner. The closest existing CLI is GitHub Copilot: `harness/copilot_runner.py` (`CopilotAgentRunner`), `harness/copilot_usage.py`, `harness/adapters/copilot.py`, `CopilotConfig` in `harness/config.py`. Copy their structure; do not refactor them. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S17 tasks exist when it runs.

Facts recorded live on 2026-11-19 (do not re-probe; tests use these as samples):
- Headless call: `kiro-cli chat --no-interactive --output-format stream-json --model <id> --trust-workspace [--trust-all-tools | --trust-tools=fs_read,execute_bash]`, prompt on stdin, cwd = workspace. stdout is JSON Lines; a denied tool also prints a plain non-JSON line `[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.`
- Models (`kiro-cli chat --list-models --format json`): auto, claude-sonnet-4.5, claude-haiku-4.5, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. The models report `"reasoning":{"support":"unavailable"}`, so `--effort` is never passed.
- Plan: KIRO FREE, 50 credits per month. `/usage` costs no credits.

Recorded `/usage` output (plain text, `kiro-cli chat --no-interactive "/usage"`):
```
Estimated Usage | resets on 2026-12-01 | KIRO FREE
Credits (0.20 of 50 covered in plan), 0.4%
Manage your plan at https://app.kiro.dev/account/usage
```

# Decisions (do not reopen)

1. `@dataclass(frozen=True, slots=True) class KiroQuota: plan: str; used: float; limit: float; used_percent: float; reset_time: datetime` (aware, UTC).
2. `class KiroUsageError(RuntimeError)`.
3. `DEFAULT_COMMAND = ["kiro-cli", "chat", "--no-interactive", "/usage"]`.
4. `parse_kiro_usage(text: str) -> KiroQuota`: strip ANSI escape sequences first. Reset: `resets on (\d{4}-\d{2}-\d{2})` gives midnight UTC of that day. Plan: the text after the last `|` on the line containing `Estimated Usage`, stripped (for the sample: `KIRO FREE`). Credits: `Credits \(([\d.]+) of ([\d.]+) covered in plan\)`. `used_percent = min(100.0, used / limit * 100)`; limit must be > 0. Any missing piece raises `KiroUsageError` naming it (`missing reset date`, `missing credits`, `missing plan`, `invalid limit`).
5. `fetch_kiro_usage(command: list[str] | None = None, timeout: int = 60, run=subprocess.run) -> KiroQuota`: same shape as `fetch_copilot_quota` (stdout/stderr pipes, text, `check=False`, `shell=False`, `encoding="utf-8"`, `errors="replace"`); timeout, `OSError` and a non-zero exit raise `KiroUsageError` with the first stderr line or a fallback. Use `prepare_external_argv` from `harness/process_utils.py` so a `.cmd`/`.exe` shim resolves on Windows, as other runners do.

# Acceptance criteria

1. The recorded sample parses to plan `KIRO FREE`, used 0.2, limit 50, used_percent 0.4, reset 2026-12-01T00:00:00+00:00.
2. The sample wrapped in ANSI color codes parses the same.
3. Each missing piece raises `KiroUsageError` with the message from decision 4.
4. A fake `run` returning exit 1 with stderr `boom` raises `KiroUsageError("boom")`; a fake raising `TimeoutExpired` raises `KiroUsageError`.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes and the recorded samples in this brief.
- Never read, print, log or store tokens, credentials or the signed-in account's e-mail. Never change the user's Kiro, agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Kiro stays off by default (`kiro.enabled: false`); nothing changes for a project that does not enable it.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
