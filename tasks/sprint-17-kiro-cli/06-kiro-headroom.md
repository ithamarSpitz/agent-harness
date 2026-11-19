---
id: S17-T6
stage: 3
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S17-T5
allowed_paths:
  - harness/headroom.py
  - harness/quota.py
  - tests/harness/test_kiro_headroom.py
  - tests/harness/test_headroom.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Routing sees Kiro's monthly credits -->
# Goal

The headroom snapshot used by routing has a `kiro` entry (one `monthly` window), cached like Copilot's, and the quota wait knows when Kiro's credits come back.

# Background

Sprint S17 adds Kiro CLI (`kiro-cli`, version 3.1.0, installed at `%LOCALAPPDATA%/Kiro-Cli/kiro-cli.exe`, on PATH) as the first CLI written the new way after S16 (design doc "Harness Redesign", section "Providers as adapters"). S16 added `harness/adapters/` (`base.py`: `BaseAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault`, `command_version`; `AdapterRegistry` in `__init__.py`) and one adapter per CLI that wraps its runner. The closest existing CLI is GitHub Copilot: `harness/copilot_runner.py` (`CopilotAgentRunner`), `harness/copilot_usage.py`, `harness/adapters/copilot.py`, `CopilotConfig` in `harness/config.py`. Copy their structure; do not refactor them. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S17 tasks exist when it runs.

Facts recorded live on 2026-11-19 (do not re-probe; tests use these as samples):
- Headless call: `kiro-cli chat --no-interactive --output-format stream-json --model <id> --trust-workspace [--trust-all-tools | --trust-tools=fs_read,execute_bash]`, prompt on stdin, cwd = workspace. stdout is JSON Lines; a denied tool also prints a plain non-JSON line `[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.`
- Models (`kiro-cli chat --list-models --format json`): auto, claude-sonnet-4.5, claude-haiku-4.5, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. The models report `"reasoning":{"support":"unavailable"}`, so `--effort` is never passed.
- Plan: KIRO FREE, 50 credits per month. `/usage` costs no credits.

# Decisions (do not reopen)

1. `headroom.py`: `_kiro_entries(source, command) -> list[Headroom]` gives `[Headroom("kiro", used_percent, int(reset_time.timestamp()), "monthly", True)]`, or `[_unknown("kiro", "monthly")]` on any error. `headroom_snapshot` adds it when `_kiro_settings` says enabled, with the same memory and disk cache handling as Copilot (`kiro_enabled` joins the cache key; `_decode_snapshot` validates the `kiro` entry like `copilot`).
2. `quota.py`: when Kiro is enabled and `used_percent >= 100`, it is reported like an exhausted Copilot (provider `kiro`, reset at `reset_time`).
3. APIs from earlier tasks: `harness/kiro_usage.py` (S17-T1) has `fetch_kiro_usage(command: list[str] | None = None, timeout: int = 60, run=subprocess.run) -> KiroQuota` with `KiroQuota.used_percent: float` and `KiroQuota.reset_time: datetime` (aware UTC) and raises `KiroUsageError`; `harness/limits.py` (S17-T5) has `_kiro_settings(config=None, *, root=None, config_path=None) -> tuple[bool, list[str] | None]` (enabled, quota command), shaped like `_copilot_settings`.
4. Nothing else in routing changes: `choose_provider` already handles any provider name with windows.

# Acceptance criteria

1. A snapshot with Kiro enabled has `kiro` with one `monthly` entry from a fake source.
2. A failing source gives an unknown entry, not an exception.
3. A cached snapshot written without `kiro` is not reused once Kiro is enabled (and the reverse).
4. 100% used gives a quota wait for `kiro` at the reset time.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes and the recorded samples in this brief.
- Never read, print, log or store tokens, credentials or the signed-in account's e-mail. Never change the user's Kiro, agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Kiro stays off by default (`kiro.enabled: false`); nothing changes for a project that does not enable it.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
