---
id: S10-T6
stage: 2
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: 
  - S10-T2
allowed_paths:
  - harness/limits.py
  - harness/headroom.py
  - tests/harness/test_agy_limits.py
  - tests/harness/test_limits.py
  - tests/harness/test_headroom.py
  - tests/harness/test_live_limits_isolated.py
  - tests/harness/test_board_headroom.py
  - tests/harness/test_pace.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Antigravity in harness limits and the headroom snapshot -->
# Goal

`harness limits` shows the Antigravity pools, and the S9 headroom snapshot includes them, so provider choice can use Antigravity's live quota like the others.

# Decisions (do not reopen)

1. `harness/limits.py`: when `agy.enabled`, add an Antigravity section with one line per pool: group name, percent left, and the reset time in local time (same format as the other providers). A failed read prints one line `Antigravity: usage unavailable (<reason>)`.
2. `harness/headroom.py`: `headroom_snapshot` adds one `Headroom` per pool for provider `agy` (used percent = 100 - remaining percent, window `weekly`, `resets_at`), from `fetch_agy_usage` (S10-T2), cached like the other sources; a failed read gives `known=False`, never an exception.
3. The agy fetch is injectable; tests use fakes.
- Config source (operator, Nov 4 2026, from the attempt-1 review): `run_limits` and `headroom_snapshot` take an optional `config` (an already loaded config) or `root` / `config_path`; with none given they keep today's default (`harness.yaml` in the current folder). A `harness.yaml` that exists but fails to load logs one warning naming the error and treats agy as disabled; it is never silent. Passing the config from the callers (`harness/cli.py`, `harness/provider_runner.py`, `harness/board.py`) is S10-T7's job, not this task's; do not edit them. Tests cover: explicit config with agy enabled from another working folder, and the broken-file warning.

# Acceptance criteria

1. With a fake usage result, `harness limits` prints both pools with percent and local reset time; with a failing fake it prints the one-line message.
2. The headroom snapshot holds two `agy` entries with the right used percent, and `known=False` on failure.
3. With `agy.enabled: false`, output and snapshot are unchanged.

# Context

The user has ChatGPT Plus (Codex), Cursor ($20) and Claude Pro (Claude Code, added in S9), and a free Google Antigravity account. Antigravity has a command-line program, `agy` (version 1.4.12 on Nov 3 2026, installed at `%LOCALAPPDATA%\agy\bin\agy.exe` and on the user PATH, signed in with the user's Google account). S10 adds it as a fourth provider that works exactly like the others: same parallelism, chosen per call by the S9 headroom/urgency rule (`harness/headroom.py`, `harness/routing.py`), same retries, fallbacks and waits. Facts measured on this machine on Nov 3 2026:

- Models (`agy models`, tab-separated `id<TAB>name`, no JSON option): `gemini-3.8-flash-high`, `gemini-3.8-flash-medium`, `gemini-3.8-flash-low`, `claude-sonnet-4-6`, `claude-opus-4-6-thinking`, `gpt-oss-120b-medium`.
- Quota: `agy -p "/usage" --output-format json` answers instantly and uses no tokens. `command.data.groups[]` has two groups, each sharing one weekly limit: "Gemini Models" (bucket id `gemini-weekly`) and "Claude and GPT models" (bucket id `3p-weekly`). Each bucket has `remaining_fraction` (0..1), `window` (`weekly`) and `reset_time` (UTC ISO). Quota is used in proportion to token cost.
- Headless runs work on Windows only with stdin closed or fully written and closed (otherwise agy waits for input). `--print-timeout` defaults to 0 (no limit); the harness's own timeout governs.
- `--output-format json` result: `{"conversation_id","status":"SUCCESS"|...,"response","duration_seconds","num_turns","usage":{"input_tokens","output_tokens","thinking_tokens","cache_read_tokens","total_tokens"},"denied_actions":[{"action","display_name"}]}`.
- Permissions: headless agy cannot ask. A tool that is not allowed is auto-denied and listed in `denied_actions`, with a stderr line starting `jetski: no output produced — a tool required the "<perm>" permission`, while the exit code stays 0 and `status` stays `SUCCESS`. `--mode accept-edits` allows file reads and edits; shell commands need rules such as `command(node test.mjs)` passed with `--settings '<json>'` (`{"permissions":{"allow":[...]}}`, for this session only, never saved).
- Other flags: `--model`, `--effort low|medium|high|xhigh|max`, `--mode accept-edits|auto|plan`, `--sandbox`, `--add-dir`, `--log-file`, `--input-format stream-json` (needs `--output-format stream-json`; one NDJSON message per stdin line), `--json-schema`, `--dangerously-skip-permissions` (not used by the harness).
- Limit texts found in the binary: `Your AI credits balance is too low to continue.`, `AI: Out of credits`, `Quota exhausted`, `RESOURCE_EXHAUSTED`, `rate limit`, `model capacity exhausted` (capacity is temporary; credits/quota is a cap until the reset).

# Rules for every S10 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task that records a sample may run the real `agy` once, as its brief says.
- Never read, print, log or store tokens or credentials. agy uses the user's own login; the harness only starts the official `agy` program and never reads `~/.gemini/` files.
- Never change the user's agy configuration (`~/.gemini/antigravity-cli/settings.json`, `agy mcp add`, `agy install`); per-call settings go through `--settings`.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
