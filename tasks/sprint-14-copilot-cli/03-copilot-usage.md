---
id: S14-T3
stage: 1
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/copilot_usage.py
  - tests/harness/test_copilot_usage.py
  - tests/harness/fixtures/copilot-user-sample.json
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Read Copilot premium-request quota -->
# Goal

The harness can read how many Copilot premium requests are left and when they reset.

# Background

Sprint S14 adds GitHub Copilot CLI as a fifth provider (after Codex, Cursor, Claude Code, agy), the same way S10 added agy (`tasks/sprint-10-antigravity/`). S15 later moves all providers into one adapter shape, so follow the existing agy code (`harness/agy_runner.py`, `harness/agy_usage.py`) rather than inventing a new structure.

Facts checked on this machine on 2026-11-07 (Copilot CLI 1.0.107, installed at `%LOCALAPPDATA%/GitHubCopilotCLI/copilot.exe`, on the user PATH as `copilot`):
- Non-interactive call: `copilot -p "<prompt>" --output-format json` prints JSONL, one event per line. The last line is `{"type":"result","exitCode":0,"usage":{"premiumRequests":1,...}}`; the answer is `data.content` of the last `{"type":"assistant.message"}` event; `{"type":"model.call_final_result","data":{"model":"gpt-5.6-luna","result":"success"}}` names the model used. Other event types (`session.*`, `assistant.message_delta`, `commands.changed`, ...) are ignored.
- `--model auto` works; `--model gpt-5-mini` fails with `Error: Model "gpt-5-mini" from --model flag is not available.` and exit 1. Auto routing profiles are set per call with the environment variable `COPILOT_AUTO_TIER` = `efficiency`, `balance` or `intelligence` (requires model auto).
- Useful flags: `-C <dir>` working directory, `--allow-all-tools`, `--allow-tool <rule>`, `--deny-tool <rule>` (rules like `write`, `shell(git push)`, `shell(git:*)`), `--disable-builtin-mcps` (turns off the built-in GitHub MCP server, which could act on real GitHub), `--no-auto-update`, `--reasoning-effort <none|minimal|low|medium|high|xhigh|max>`, `--log-dir`. Non-interactive mode denies any tool no allow rule approves.
- Quota: `gh api copilot_internal/user` returns JSON with `copilot_plan`, `quota_reset_date_utc` (e.g. `2026-12-01T00:00:00.000Z`) and `quota_snapshots.premium_interactions` = `{"entitlement":200,"remaining":199,"percent_remaining":99.5,"unlimited":false}` (also `chat` and `completions` snapshots with `unlimited: true`). This plan has 200 premium requests a month; one `-p` call costs one.
- Out-of-quota and sign-in error texts were not observed yet; use the patterns given in the briefs and keep them in one constant each.

`harness/agy_usage.py` is the model: a parse function, a fetch function with an injectable `run`, and one small error class.

# Decisions (do not reopen)

1. New `harness/copilot_usage.py`: `@dataclass(frozen=True, slots=True) class CopilotQuota: plan: str; entitlement: int; remaining: int; percent_remaining: float; unlimited: bool; reset_time: datetime` (aware UTC); `parse_copilot_user(text: str) -> CopilotQuota` reading `quota_snapshots.premium_interactions` and `quota_reset_date_utc`; `fetch_copilot_quota(command: list[str] | None = None, timeout: int = 30, run=subprocess.run) -> CopilotQuota` (default command `["gh", "api", "copilot_internal/user"]`); `class CopilotUsageError(RuntimeError)`.
2. Missing or malformed fields raise `CopilotUsageError` with a short reason; a failed or timed-out run raises it with the first line of stderr only. Never include the raw response in errors or logs (it can hold account details).
3. The fixture `tests/harness/fixtures/copilot-user-sample.json` is written by hand from the shape in Background (plan `individual`, entitlement 200, remaining 199, percent 99.5, reset `2026-12-01T00:00:00.000Z`) with no login, email or ids. Do not call the real `gh`.

# Acceptance criteria

1. Tests in `tests/harness/test_copilot_usage.py` parse the fixture to the expected `CopilotQuota`.
2. Tests: missing `premium_interactions`, bad JSON, and a non-zero exit from a fake `run` each raise `CopilotUsageError`; a timeout from the fake raises it too.
3. A test checks the fake `run` received the default command and no shell.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and the fixtures named here.
- Never read, print, log or store tokens or credentials. Never change the user's Copilot configuration (`~/.copilot`); per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
