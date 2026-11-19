---
id: S17-T2
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/config.py
  - tests/harness/test_config_kiro.py
  - tests/harness/test_config.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Kiro section in harness.yaml -->
# Goal

`HarnessConfig` gets a `kiro` section (`KiroConfig`), off by default, validated like the `copilot` section.

# Background

Sprint S17 adds Kiro CLI (`kiro-cli`, version 3.1.0, installed at `%LOCALAPPDATA%/Kiro-Cli/kiro-cli.exe`, on PATH) as the first CLI written the new way after S16 (design doc "Harness Redesign", section "Providers as adapters"). S16 added `harness/adapters/` (`base.py`: `BaseAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault`, `command_version`; `AdapterRegistry` in `__init__.py`) and one adapter per CLI that wraps its runner. The closest existing CLI is GitHub Copilot: `harness/copilot_runner.py` (`CopilotAgentRunner`), `harness/copilot_usage.py`, `harness/adapters/copilot.py`, `CopilotConfig` in `harness/config.py`. Copy their structure; do not refactor them. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S17 tasks exist when it runs.

Facts recorded live on 2026-11-19 (do not re-probe; tests use these as samples):
- Headless call: `kiro-cli chat --no-interactive --output-format stream-json --model <id> --trust-workspace [--trust-all-tools | --trust-tools=fs_read,execute_bash]`, prompt on stdin, cwd = workspace. stdout is JSON Lines; a denied tool also prints a plain non-JSON line `[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.`
- Models (`kiro-cli chat --list-models --format json`): auto, claude-sonnet-4.5, claude-haiku-4.5, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. The models report `"reasoning":{"support":"unavailable"}`, so `--effort` is never passed.
- Plan: KIRO FREE, 50 credits per month. `/usage` costs no credits.

# Decisions (do not reopen)

1. `@dataclass(slots=True) class KiroConfig: enabled: bool = False; command: str = "kiro-cli"; implementation_sequence: list[str] = ["glm-5", "claude-sonnet-4.5", "claude-sonnet-4.5"]; review_model: str = "claude-sonnet-4.5"; escalation_model: str = "claude-sonnet-4.5"; quota_command: list[str] = ["kiro-cli", "chat", "--no-interactive", "/usage"]` (use `field(default_factory=...)` for lists).
2. `HarnessConfig.kiro: KiroConfig = field(default_factory=KiroConfig)`, loaded by a new `_load_kiro(raw)` called from `HarnessConfig.load` next to `_load_copilot`. The machine config merge from S16 applies as for every section.
3. Validation, each as `ValueError` naming the key: section must be a mapping; `enabled` must be a bool; `command`, `review_model`, `escalation_model` must be non-empty strings; `implementation_sequence` must be a non-empty list of non-empty strings (`kiro.implementation_sequence[1] ...`); `quota_command` must be a non-empty list of strings; unknown keys are rejected (`kiro.foo is not a known key`).
4. `review_model: auto` is rejected (`kiro.review_model must not be auto: Auto never reviews`), because the model behind Auto is unknown.

# Acceptance criteria

1. No `kiro` section gives the defaults from decision 1.
2. A full valid section loads every field.
3. Each invalid case in decisions 3 and 4 raises `ValueError` with the key in the message.
4. A machine config with `kiro.enabled: true` and a project file without `kiro` gives `enabled == True`.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes and the recorded samples in this brief.
- Never read, print, log or store tokens, credentials or the signed-in account's e-mail. Never change the user's Kiro, agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Kiro stays off by default (`kiro.enabled: false`); nothing changes for a project that does not enable it.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
