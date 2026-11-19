---
id: S17-T4
stage: 2
model_class: hard_worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S17-T2
  - S17-T3
allowed_paths:
  - harness/kiro_runner.py
  - tests/harness/test_kiro_runner.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Kiro runner: implement, review, plan repair, brief check -->
# Goal

A new `harness/kiro_runner.py` with `KiroAgentRunner` runs Kiro for implement, review, plan_change and brief_check, the way `CopilotAgentRunner` runs Copilot.

# Background

Sprint S17 adds Kiro CLI (`kiro-cli`, version 3.1.0, installed at `%LOCALAPPDATA%/Kiro-Cli/kiro-cli.exe`, on PATH) as the first CLI written the new way after S16 (design doc "Harness Redesign", section "Providers as adapters"). S16 added `harness/adapters/` (`base.py`: `BaseAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault`, `command_version`; `AdapterRegistry` in `__init__.py`) and one adapter per CLI that wraps its runner. The closest existing CLI is GitHub Copilot: `harness/copilot_runner.py` (`CopilotAgentRunner`), `harness/copilot_usage.py`, `harness/adapters/copilot.py`, `CopilotConfig` in `harness/config.py`. Copy their structure; do not refactor them. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S17 tasks exist when it runs.

Facts recorded live on 2026-11-19 (do not re-probe; tests use these as samples):
- Headless call: `kiro-cli chat --no-interactive --output-format stream-json --model <id> --trust-workspace [--trust-all-tools | --trust-tools=fs_read,execute_bash]`, prompt on stdin, cwd = workspace. stdout is JSON Lines; a denied tool also prints a plain non-JSON line `[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.`
- Models (`kiro-cli chat --list-models --format json`): auto, claude-sonnet-4.5, claude-haiku-4.5, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. The models report `"reasoning":{"support":"unavailable"}`, so `--effort` is never passed.
- Plan: KIRO FREE, 50 credits per month. `/usage` costs no credits.

# Decisions (do not reopen)

1. Same public surface as `CopilotAgentRunner`: `__init__(config, log_dir, context=None, usage=None)`, `supports_ui_review = False`, `availability(*, refresh=False) -> tuple[bool, str]`, `implement(...)`, `review(...)`, `plan_change(..., model_class="reviewer", env=None)`, `brief_check(...)`, same signatures and same prompt builders (`implement_prompt`, `review_prompt`, `plan_change_prompt`, `readthrough_prompt`).
2. Command: `[exe, "chat", "--no-interactive", "--output-format", "stream-json", "--model", model, "--trust-workspace"]` plus `"--trust-all-tools"` for implement, or `"--trust-tools=fs_read,execute_bash"` for review, plan_change and brief_check. Never `--effort`. The prompt goes to stdin (written, then stdin closed); `cwd=workspace`. `exe` is `kiro.command` if absolute, else `locate(kiro.command)`; not found gives a `launch_failure` result with `provider_could_not_start("kiro", KIRO_NOT_FOUND)`, like Copilot.
3. Models: implement uses `implementation_sequence[min(attempt - 1, len - 1)]`; `model_class` `escalation` (from `model_class_override` or `task.model_class`) uses `escalation_model`. Review and brief_check use `review_model`; plan_change uses `escalation_model` for `escalation`, else `review_model`.
4. Process handling (Popen, output reader thread, stall and timeout, `stop_process_tree`, `on_process_start`, Windows process group, log file per call) copies `CopilotAgentRunner._invoke`. Log names: `{task.id}-attempt-{attempt}-kiro.log`, `{task.id}-review-kiro.log`, `{task.id}-plan-repair-kiro.log`, `{task.id}-brief-check-kiro.log`.
5. Result: parse stdout with `parse_kiro_stream`, flags with `classify_kiro` (both from `harness/kiro_stream.py`, S17-T3; use them as they are, add no patterns or event types here; tests may build streams from the samples in S17-T3's brief or call `parse_kiro_stream` on small fake JSON Lines). `ok` only when the exit code is 0, `status == "success"` and no `error`. `output` is `final_text`. Flags map to `AgentResult` fields: quota -> `quota_exhausted`, auth -> `auth_failure`, model_unavailable -> `model_unavailable`, config -> `config_fault`, capacity -> `capacity_exhausted`, transient -> `transient_error`. `provider="kiro"`, `model=<model>`.
6. Missing report: an `ok` implement whose output has no `STATUS:` line, or an `ok` review without `VERDICT:`, becomes `ok=False`, `launch_failure=True`, error `kiro finished without a STATUS report ({n} denied tool calls)` (or `VERDICT`), and the last 40 output lines are written to the call's log. Same rule as agy in S15-T5.
7. Usage event like `CopilotAgentRunner._record_usage`, with `"provider": "kiro"`, `"model_class": "kiro"`, `"credits": <stream.credits>` instead of `premium_requests`, no `auto_tier`/`effort` keys.

# Acceptance criteria

1. Fake Popen tests check the exact command for implement and for review, and that the prompt arrives on stdin, not in argv.
2. Attempt 1 uses `glm-5`, attempt 2 and 3 `claude-sonnet-4.5`; `escalation` uses `escalation_model`.
3. A fake success stream with `STATUS: done` gives `ok`, output, provider `kiro` and a usage event with `credits`.
4. A fake success stream without `STATUS:` gives `launch_failure` with the decision 6 message, counting `[denied]` lines.
5. The unknown-model sample gives `model_unavailable`; a quota message gives `quota_exhausted`.
6. Command not found gives `launch_failure` without starting a process.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes and the recorded samples in this brief.
- Never read, print, log or store tokens, credentials or the signed-in account's e-mail. Never change the user's Kiro, agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Kiro stays off by default (`kiro.enabled: false`); nothing changes for a project that does not enable it.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
