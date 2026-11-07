---
id: S14-T7
stage: 4
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S14-T5
  - S14-T6
allowed_paths:
  - harness/provider_runner.py
  - harness/cli.py
  - tests/harness/test_copilot_wiring.py
  - tests/harness/test_provider_choice.py
  - tests/harness/test_routing.py
  - tests/harness/test_brief_check_cli.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Route tasks to Copilot -->
# Goal

When `copilot.enabled`, the harness can choose Copilot to implement, review, analyse and brief-check, following the routing rules.

# Background

Sprint S14 adds GitHub Copilot CLI as a fifth provider (after Codex, Cursor, Claude Code, agy), the same way S10 added agy (`tasks/sprint-10-antigravity/`). S15 later moves all providers into one adapter shape, so follow the existing agy code (`harness/agy_runner.py`, `harness/agy_usage.py`) rather than inventing a new structure.

Facts checked on this machine on 2026-11-07 (Copilot CLI 1.0.107, installed at `%LOCALAPPDATA%/GitHubCopilotCLI/copilot.exe`, on the user PATH as `copilot`):
- Non-interactive call: `copilot -p "<prompt>" --output-format json` prints JSONL, one event per line. The last line is `{"type":"result","exitCode":0,"usage":{"premiumRequests":1,...}}`; the answer is `data.content` of the last `{"type":"assistant.message"}` event; `{"type":"model.call_final_result","data":{"model":"gpt-5.6-luna","result":"success"}}` names the model used. Other event types (`session.*`, `assistant.message_delta`, `commands.changed`, ...) are ignored.
- `--model auto` works; `--model gpt-5-mini` fails with `Error: Model "gpt-5-mini" from --model flag is not available.` and exit 1. Auto routing profiles are set per call with the environment variable `COPILOT_AUTO_TIER` = `efficiency`, `balance` or `intelligence` (requires model auto).
- Useful flags: `-C <dir>` working directory, `--allow-all-tools`, `--allow-tool <rule>`, `--deny-tool <rule>` (rules like `write`, `shell(git push)`, `shell(git:*)`), `--disable-builtin-mcps` (turns off the built-in GitHub MCP server, which could act on real GitHub), `--no-auto-update`, `--reasoning-effort <none|minimal|low|medium|high|xhigh|max>`, `--log-dir`. Non-interactive mode denies any tool no allow rule approves.
- Quota: `gh api copilot_internal/user` returns JSON with `copilot_plan`, `quota_reset_date_utc` (e.g. `2026-12-01T00:00:00.000Z`) and `quota_snapshots.premium_interactions` = `{"entitlement":200,"remaining":199,"percent_remaining":99.5,"unlimited":false}` (also `chat` and `completions` snapshots with `unlimited: true`). This plan has 200 premium requests a month; one `-p` call costs one.
- Out-of-quota and sign-in error texts were not observed yet; use the patterns given in the briefs and keep them in one constant each.

`harness/provider_runner.py` builds the agy runner and checks `_agy_enabled_for` (around line 243); `harness/routing.py` `choose_provider` applies the S9-T32 rules (order, waste first, Claude caps, reviewer different from implementer unless only the implementer has usage). `tests/harness/test_agy_wiring.py` shows the wiring tests for agy.

# Decisions (do not reopen)

1. `build_runtime` in `harness/cli.py` builds `CopilotAgentRunner` when `copilot.enabled` and passes it to `ProviderAgentRunner` (in `harness/provider_runner.py`) next to the others; per-task disable keys, `reset-task` and `run-task --fresh-budget` clearing work as for agy.
2. `choose_provider` treats `copilot` like the others; its usage comes from the `copilot` headroom entry (S14-T6). Quota, auth and launch failures disable it for the task.
3. `harness brief-check` (`cmd_brief_check` in `harness/cli.py`, which today calls `runtime.runner.codex` directly) goes through a new `ProviderAgentRunner.brief_check` that chooses between the providers that have `brief_check` (Codex and Copilot only) with the same rules as plan-change. Exit codes stay as in S13-T5.
4. `harness/routing.py` should need no change; if it does, stop and report.
5. With `copilot.enabled: false`, routing and brief-check are exactly as before (existing tests unchanged).

# Acceptance criteria

1. `tests/harness/test_copilot_wiring.py`: with fakes, Copilot first in priority implements; the reviewer is another provider with usage; with only Copilot having usage it reviews its own work; a quota failure moves the task to the next provider.
2. Existing tests in `test_provider_choice.py`, `test_routing.py` and `test_brief_check_cli.py` pass unchanged or with one added Copilot case each.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and the fixtures named here.
- Never read, print, log or store tokens or credentials. Never change the user's Copilot configuration (`~/.copilot`); per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
