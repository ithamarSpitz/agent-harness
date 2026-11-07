---
id: S14-T1
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/config.py
  - tests/harness/test_config_copilot.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- copilot: section in harness.yaml config -->
# Goal

`harness.yaml` can describe the Copilot CLI provider; nothing uses it yet.

# Background

Sprint S14 adds GitHub Copilot CLI as a fifth provider (after Codex, Cursor, Claude Code, agy), the same way S10 added agy (`tasks/sprint-10-antigravity/`). S15 later moves all providers into one adapter shape, so follow the existing agy code (`harness/agy_runner.py`, `harness/agy_usage.py`) rather than inventing a new structure.

Facts checked on this machine on 2026-11-07 (Copilot CLI 1.0.107, installed at `%LOCALAPPDATA%/GitHubCopilotCLI/copilot.exe`, on the user PATH as `copilot`):
- Non-interactive call: `copilot -p "<prompt>" --output-format json` prints JSONL, one event per line. The last line is `{"type":"result","exitCode":0,"usage":{"premiumRequests":1,...}}`; the answer is `data.content` of the last `{"type":"assistant.message"}` event; `{"type":"model.call_final_result","data":{"model":"gpt-5.6-luna","result":"success"}}` names the model used. Other event types (`session.*`, `assistant.message_delta`, `commands.changed`, ...) are ignored.
- `--model auto` works; `--model gpt-5-mini` fails with `Error: Model "gpt-5-mini" from --model flag is not available.` and exit 1. Auto routing profiles are set per call with the environment variable `COPILOT_AUTO_TIER` = `efficiency`, `balance` or `intelligence` (requires model auto).
- Useful flags: `-C <dir>` working directory, `--allow-all-tools`, `--allow-tool <rule>`, `--deny-tool <rule>` (rules like `write`, `shell(git push)`, `shell(git:*)`), `--disable-builtin-mcps` (turns off the built-in GitHub MCP server, which could act on real GitHub), `--no-auto-update`, `--reasoning-effort <none|minimal|low|medium|high|xhigh|max>`, `--log-dir`. Non-interactive mode denies any tool no allow rule approves.
- Quota: `gh api copilot_internal/user` returns JSON with `copilot_plan`, `quota_reset_date_utc` (e.g. `2026-12-01T00:00:00.000Z`) and `quota_snapshots.premium_interactions` = `{"entitlement":200,"remaining":199,"percent_remaining":99.5,"unlimited":false}` (also `chat` and `completions` snapshots with `unlimited: true`). This plan has 200 premium requests a month; one `-p` call costs one.
- Out-of-quota and sign-in error texts were not observed yet; use the patterns given in the briefs and keep them in one constant each.

`AgyConfig` in `harness/config.py` (around line 169) is the model to copy: dataclass, loader, validation, and `providers.priority` names.

# Decisions (do not reopen)

1. New `CopilotConfig` loaded from a `copilot:` section, like `agy:`:
    - `enabled: false`, `command: copilot`,
    - `implementation_sequence`: list of `{model, auto_tier, effort}` (`auto_tier` and `effort` optional), one per attempt; default `[{model: auto, auto_tier: balance}, {model: auto, auto_tier: intelligence}, {model: auto, auto_tier: intelligence, effort: high}]`,
    - `review_model: auto`, `review_auto_tier: intelligence`, `review_effort: null`,
    - `escalation_model: auto`, `escalation_auto_tier: intelligence`, `escalation_effort: high`,
    - `quota_command: [gh, api, copilot_internal/user]`.
2. `providers.priority` accepts `copilot`.
3. Validation errors: empty sequence; `auto_tier` not one of `efficiency`, `balance`, `intelligence`; `auto_tier` given with a model other than `auto`; `effort` not one of `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max`; non-string values (checked first).

# Acceptance criteria

1. `tests/harness/test_config_copilot.py`: defaults load with no `copilot:` section; a full section round-trips; each validation error above is raised with a message naming the bad value.
2. `providers.priority: [copilot, codex]` loads.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and the fixtures named here.
- Never read, print, log or store tokens or credentials. Never change the user's Copilot configuration (`~/.copilot`); per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
