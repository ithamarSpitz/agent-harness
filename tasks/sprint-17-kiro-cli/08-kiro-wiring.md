---
id: S17-T8
stage: 4
model_class: hard_worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S17-T6
  - S17-T7
allowed_paths:
  - harness/provider_runner.py
  - harness/cli.py
  - tests/harness/test_kiro_wiring.py
  - tests/harness/test_adapter_boundaries.py
  - tests/harness/test_provider_registry.py
  - tests/harness/test_provider_choice.py
  - tests/harness/test_copilot_wiring.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Router and runtime use Kiro when enabled -->
# Goal

With `kiro.enabled: true` the runtime builds a `KiroAgentRunner`, the router registers `KiroAdapter`, and routing can choose Kiro for implement, review, plan repair and brief check like any other provider.

# Background

Sprint S17 adds Kiro CLI (`kiro-cli`, version 3.1.0, installed at `%LOCALAPPDATA%/Kiro-Cli/kiro-cli.exe`, on PATH) as the first CLI written the new way after S16 (design doc "Harness Redesign", section "Providers as adapters"). S16 added `harness/adapters/` (`base.py`: `BaseAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault`, `command_version`; `AdapterRegistry` in `__init__.py`) and one adapter per CLI that wraps its runner. The closest existing CLI is GitHub Copilot: `harness/copilot_runner.py` (`CopilotAgentRunner`), `harness/copilot_usage.py`, `harness/adapters/copilot.py`, `CopilotConfig` in `harness/config.py`. Copy their structure; do not refactor them. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S17 tasks exist when it runs.

Facts recorded live on 2026-11-19 (do not re-probe; tests use these as samples):
- Headless call: `kiro-cli chat --no-interactive --output-format stream-json --model <id> --trust-workspace [--trust-all-tools | --trust-tools=fs_read,execute_bash]`, prompt on stdin, cwd = workspace. stdout is JSON Lines; a denied tool also prints a plain non-JSON line `[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.`
- Models (`kiro-cli chat --list-models --format json`): auto, claude-sonnet-4.5, claude-haiku-4.5, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. The models report `"reasoning":{"support":"unavailable"}`, so `--effort` is never passed.
- Plan: KIRO FREE, 50 credits per month. `/usage` costs no credits.

# Decisions (do not reopen)

1. `cli.build_runtime` creates `KiroAgentRunner(config, runtime_root / "logs", context, usage)` when `config.kiro.enabled`, else None, and passes `kiro=` to `ProviderAgentRunner`.
2. `ProviderAgentRunner.__init__` takes `kiro: KiroAgentRunner | None = None` (keyword, last) and registers `KiroAdapter(kiro, config)` after Claude.
3. The fixed provider tuples in `provider_runner.py` (`_disabled_for`, and the implementer set in `implement`) become `self.adapters.names()`, so a new adapter needs no edit there.
4. `plan_change`'s gate `self.config.claude.enabled or self.config.copilot.enabled` also routes when `self.config.kiro.enabled`.
5. `tests/harness/test_adapter_boundaries.py` adds `kiro` to `PROVIDER_NAMES`.
6. Kiro is chosen only through routing (priority and headroom); the routing-off path in `_preferred_provider` is unchanged.

# Acceptance criteria

1. With Kiro enabled and first in `providers.priority`, a fake Kiro runner gets the implement call; review goes to another provider when one has quota.
2. With Kiro disabled, no Kiro runner is built and behavior is unchanged (existing tests pass).
3. A Kiro `quota_exhausted` result disables Kiro for the task and falls back to the next provider.
4. The boundary test passes with `kiro` in `PROVIDER_NAMES`.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes and the recorded samples in this brief.
- Never read, print, log or store tokens, credentials or the signed-in account's e-mail. Never change the user's Kiro, agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Kiro stays off by default (`kiro.enabled: false`); nothing changes for a project that does not enable it.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
