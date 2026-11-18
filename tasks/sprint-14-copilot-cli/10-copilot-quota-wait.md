---
id: S14-T10
stage: 6
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on:
  - S14-T8
allowed_paths:
  - harness/quota.py
  - harness/cli.py
  - tests/harness/test_copilot_quota_wait.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Quota waits use Copilot's reset time -->
# Goal

When the harness waits for quota, an exhausted Copilot counts: the wait can end at Copilot's monthly reset.

# Background

Sprint S14 adds GitHub Copilot CLI as a fifth provider (after Codex, Cursor, Claude Code, agy), the same way S10 added agy (`tasks/sprint-10-antigravity/`). S15 later moves all providers into one adapter shape, so follow the existing agy code (`harness/agy_runner.py`, `harness/agy_usage.py`) rather than inventing a new structure.

Facts checked on this machine on 2026-11-07 (Copilot CLI 1.0.107, installed at `%LOCALAPPDATA%/GitHubCopilotCLI/copilot.exe`, on the user PATH as `copilot`):
- Non-interactive call: `copilot -p "<prompt>" --output-format json` prints JSONL, one event per line. The last line is `{"type":"result","exitCode":0,"usage":{"premiumRequests":1,...}}`; the answer is `data.content` of the last `{"type":"assistant.message"}` event; `{"type":"model.call_final_result","data":{"model":"gpt-5.6-luna","result":"success"}}` names the model used. Other event types (`session.*`, `assistant.message_delta`, `commands.changed`, ...) are ignored.
- `--model auto` works; `--model gpt-5-mini` fails with `Error: Model "gpt-5-mini" from --model flag is not available.` and exit 1. Auto routing profiles are set per call with the environment variable `COPILOT_AUTO_TIER` = `efficiency`, `balance` or `intelligence` (requires model auto).
- Useful flags: `-C <dir>` working directory, `--allow-all-tools`, `--allow-tool <rule>`, `--deny-tool <rule>` (rules like `write`, `shell(git push)`, `shell(git:*)`), `--disable-builtin-mcps` (turns off the built-in GitHub MCP server, which could act on real GitHub), `--no-auto-update`, `--reasoning-effort <none|minimal|low|medium|high|xhigh|max>`, `--log-dir`. Non-interactive mode denies any tool no allow rule approves.
- Quota: `gh api copilot_internal/user` returns JSON with `copilot_plan`, `quota_reset_date_utc` (e.g. `2026-12-01T00:00:00.000Z`) and `quota_snapshots.premium_interactions` = `{"entitlement":200,"remaining":199,"percent_remaining":99.5,"unlimited":false}` (also `chat` and `completions` snapshots with `unlimited: true`). This plan has 200 premium requests a month; one `-p` call costs one.
- Out-of-quota and sign-in error texts were not observed yet; use the patterns given in the briefs and keep them in one constant each.

Found by the S14-T9 review (2026-11-09): `default_limits_source` in `harness/quota.py` (around line 89) only reads Codex and Cursor, so the README could not truthfully say Copilot takes part in quota waits. `harness/cli.py` `build_runtime` calls it in `routing_limits` (around line 149). Copilot settings: `_copilot_settings` and `load_copilot` in `harness/limits.py` (around lines 133 and 955).

# Decisions (do not reopen)

1. `default_limits_source(now, *, config=None, copilot_source=None)`: when Copilot is enabled (from `config`, or `_copilot_settings` when `config` is None), read the quota by calling the source directly: `copilot_source` defaults to `fetch_copilot_quota` from `harness/copilot_usage.py`, which returns a `CopilotQuota` with `remaining`, `unlimited` and `reset_time` (do not use `load_copilot` in `harness/limits.py`: its result drops `unlimited`); if not unlimited and `remaining <= 0`, append `QuotaResetInfo(provider="copilot", reset_at=<reset time, UTC>)`. A failed read adds nothing (same as Codex and Cursor).
2. `routing_limits` in `harness/cli.py` passes the loaded `config`.
3. Codex and Cursor behavior is unchanged.
4. `SPEC.md` is updated by S14-T9, not here.

# Acceptance criteria

1. `tests/harness/test_copilot_quota_wait.py` with a fake source: remaining 0 gives one Copilot reset; remaining 5 gives none; unlimited gives none; a failing source gives none; Copilot disabled never calls the source.
2. Existing quota tests pass unchanged.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and the fixtures named here.
- Never read, print, log or store tokens or credentials. Never change the user's Copilot configuration (`~/.copilot`); per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
