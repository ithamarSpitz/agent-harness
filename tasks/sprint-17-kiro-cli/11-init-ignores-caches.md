---
id: S17-T11
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/init_project.py
  - tests/harness/test_init_project.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- harness init ignores tool caches -->
# Goal

A project made by `harness init` ignores common tool caches, so running tests inside a task never fails the changed-files check.

# Background

Sprint S17 adds Kiro CLI (`kiro-cli`, version 3.1.0, installed at `%LOCALAPPDATA%/Kiro-Cli/kiro-cli.exe`, on PATH) as the first CLI written the new way after S16 (design doc "Harness Redesign", section "Providers as adapters"). S16 added `harness/adapters/` (`base.py`: `BaseAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault`, `command_version`; `AdapterRegistry` in `__init__.py`) and one adapter per CLI that wraps its runner. The closest existing CLI is GitHub Copilot: `harness/copilot_runner.py` (`CopilotAgentRunner`), `harness/copilot_usage.py`, `harness/adapters/copilot.py`, `CopilotConfig` in `harness/config.py`. Copy their structure; do not refactor them. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S17 tasks exist when it runs.

Facts recorded live on 2026-11-19 (do not re-probe; tests use these as samples):
- Headless call: `kiro-cli chat --no-interactive --output-format stream-json --model <id> --trust-workspace [--trust-all-tools | --trust-tools=fs_read,execute_bash]`, prompt on stdin, cwd = workspace. stdout is JSON Lines; a denied tool also prints a plain non-JSON line `[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.`
- Models (`kiro-cli chat --list-models --format json`): auto, claude-sonnet-4.5, claude-haiku-4.5, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. The models report `"reasoning":{"support":"unavailable"}`, so `--effort` is never passed.
- Plan: KIRO FREE, 50 credits per month. `/usage` costs no credits.

Found in the S16 live smoke test (2026-11-19, throwaway repo made with `harness init --no-github`): Codex implemented a tiny task correctly and ran `python -m pytest`, which created `__pycache__/`. The changed-files check in `Verifier.verify_task` (`harness/verifier.py`) counted `__pycache__/...pyc` as outside `allowed_paths` and failed the attempt; the second attempt deleted the cache by hand and passed. One attempt was wasted. The generated `.gitignore` held only `.harness/`. The failed attempt's verification log was then overwritten by the next attempt's, because the file name `{task.id}-verification.log` has no attempt number.

# Decisions (do not reopen)

1. `_update_gitignore` writes these lines in addition to `.harness/`, each only if not already present, in this order: `__pycache__/`, `*.py[cod]`, `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`, `node_modules/`.
2. An existing `.gitignore` keeps its content and order; missing lines are appended once; running init twice adds nothing.
3. The init report line for `.gitignore` stays as today (`created` or `updated`).

# Acceptance criteria

1. A new project's `.gitignore` has `.harness/` and the six lines from decision 1.
2. An existing `.gitignore` with `node_modules/` and a custom line keeps both, and gets the other lines once.
3. A second init run leaves the file unchanged.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes and the recorded samples in this brief.
- Never read, print, log or store tokens, credentials or the signed-in account's e-mail. Never change the user's Kiro, agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Kiro stays off by default (`kiro.enabled: false`); nothing changes for a project that does not enable it.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
