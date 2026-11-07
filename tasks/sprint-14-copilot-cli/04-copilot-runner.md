---
id: S14-T4
stage: 2
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S14-T1
allowed_paths:
  - harness/copilot_runner.py
  - tests/harness/test_copilot_runner.py
  - tests/harness/fixtures/copilot-stream-sample.jsonl
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Copilot runner: implement, review, plan-change -->
# Goal

A Copilot runner can implement, review and analyse a task the same way the other runners do.

# Background

Sprint S14 adds GitHub Copilot CLI as a fifth provider (after Codex, Cursor, Claude Code, agy), the same way S10 added agy (`tasks/sprint-10-antigravity/`). S15 later moves all providers into one adapter shape, so follow the existing agy code (`harness/agy_runner.py`, `harness/agy_usage.py`) rather than inventing a new structure.

Facts checked on this machine on 2026-11-07 (Copilot CLI 1.0.107, installed at `%LOCALAPPDATA%/GitHubCopilotCLI/copilot.exe`, on the user PATH as `copilot`):
- Non-interactive call: `copilot -p "<prompt>" --output-format json` prints JSONL, one event per line. The last line is `{"type":"result","exitCode":0,"usage":{"premiumRequests":1,...}}`; the answer is `data.content` of the last `{"type":"assistant.message"}` event; `{"type":"model.call_final_result","data":{"model":"gpt-5.6-luna","result":"success"}}` names the model used. Other event types (`session.*`, `assistant.message_delta`, `commands.changed`, ...) are ignored.
- `--model auto` works; `--model gpt-5-mini` fails with `Error: Model "gpt-5-mini" from --model flag is not available.` and exit 1. Auto routing profiles are set per call with the environment variable `COPILOT_AUTO_TIER` = `efficiency`, `balance` or `intelligence` (requires model auto).
- Useful flags: `-C <dir>` working directory, `--allow-all-tools`, `--allow-tool <rule>`, `--deny-tool <rule>` (rules like `write`, `shell(git push)`, `shell(git:*)`), `--disable-builtin-mcps` (turns off the built-in GitHub MCP server, which could act on real GitHub), `--no-auto-update`, `--reasoning-effort <none|minimal|low|medium|high|xhigh|max>`, `--log-dir`. Non-interactive mode denies any tool no allow rule approves.
- Quota: `gh api copilot_internal/user` returns JSON with `copilot_plan`, `quota_reset_date_utc` (e.g. `2026-12-01T00:00:00.000Z`) and `quota_snapshots.premium_interactions` = `{"entitlement":200,"remaining":199,"percent_remaining":99.5,"unlimited":false}` (also `chat` and `completions` snapshots with `unlimited: true`). This plan has 200 premium requests a month; one `-p` call costs one.
- Out-of-quota and sign-in error texts were not observed yet; use the patterns given in the briefs and keep them in one constant each.

Follow `harness/agy_runner.py` for structure and shared helpers (logging, stall watchdog, `AgentResult` fields) and `harness/codex_runner.py` `_invoke` for the subprocess pattern. Config comes from `CopilotConfig` (S14-T1).

# Decisions (do not reopen)

1. New `harness/copilot_runner.py` with `CopilotAgentRunner` exposing the same public methods and `AgentResult` fields as `AgyAgentRunner` (`implement`, `review`, `plan_change`, `availability`, plus `brief_check` like `CodexAgentRunner.brief_check`).
2. Every call: `copilot -p <prompt> --output-format json -C <worktree> --no-auto-update --disable-builtin-mcps --model <model>` plus `--reasoning-effort <effort>` when set, with `COPILOT_AUTO_TIER=<tier>` in the child environment when set (never in the parent). The prompt goes as the `-p` argument; if it is longer than 24,000 characters, write it to a file in the run log folder and pass a short prompt telling Copilot to read that file (add `--add-dir` for that folder).
3. Implement: `--allow-all-tools` plus `--deny-tool` for `shell(git push)`, `shell(git commit)`, `shell(git reset)`, `shell(git checkout)`, `shell(git rebase)` and `shell(gh)`. Review, plan-change and brief-check are read-only: `--deny-tool write` and `--allow-tool` for `shell(git diff:*)`, `shell(git log:*)`, `shell(git show:*)`, `shell(git status:*)`.
4. Parse the JSONL: answer = `data.content` of the last `assistant.message` event; model = `data.model` of the last `model.call_final_result`; success = a `result` event with `exitCode` 0 and process exit 0. Record `usage.premiumRequests` in the usage log the way other runners record usage. Non-JSON lines are ignored.
5. Model choice: implementation attempt n uses entry n of `implementation_sequence` (last entry after that); review uses the review triple; model class `escalation` uses the escalation triple.
6. `availability()` runs `copilot --version` once (located with `harness.command_locate.locate`) and caches the result like `CodexAgentRunner.availability`; it never checks sign-in (a sign-in failure shows up as a failed call, S14-T5). `brief_check` uses the review triple.
7. Usage log: one event per call shaped like `CodexAgentRunner._record_usage` with `provider: "copilot"`, `model` = the routed model from `model.call_final_result` (or the configured model if none), `requested_model` = the configured model, `auto_tier`, `effort`, and `premium_requests` = `usage.premiumRequests` (0 if missing). Token fields stay 0.
8. The fixture `tests/harness/fixtures/copilot-stream-sample.jsonl` is written by hand from the event shapes in Background (a few ignored events, one `model.call_final_result`, one `assistant.message`, one `result`). You may run the real Copilot at most twice, in an empty temporary folder, with `--model auto` and a one-line prompt, to check the shape; say so in your report. Tests never run it.

# Acceptance criteria

1. Tests in `tests/harness/test_copilot_runner.py` with a fake subprocess check the exact argument lists for implement and review, including the deny and allow rules, and that `COPILOT_AUTO_TIER` is set only in the child environment.
2. Tests parse the fixture into answer, model and premium-request count; a stream without a `result` event is a failure.
3. A test checks attempt 2 uses sequence entry 2 and attempt 5 uses the last entry.
4. A test checks a prompt over 24,000 characters goes through a file.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and the fixtures named here.
- Never read, print, log or store tokens or credentials. Never change the user's Copilot configuration (`~/.copilot`); per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
