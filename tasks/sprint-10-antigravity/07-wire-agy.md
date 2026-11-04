---
id: S10-T7
stage: 4
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: true
depends_on: 
  - S10-T4
  - S10-T5
  - S10-T6
allowed_paths:
  - harness/provider_runner.py
  - harness/routing.py
  - harness/cli.py
  - tests/harness/test_agy_wiring.py
  - tests/harness/test_provider_choice.py
  - tests/harness/test_routing.py
  - tests/harness/test_routing_scenarios.py
  - tests/harness/test_doctor_recovery.py
  - harness.yaml
  - tests/harness/test_cursor_auto_when_exhausted.py
  - tests/harness/test_board_headroom.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Use Antigravity like the other providers -->
# Goal

With `agy.enabled: true`, Antigravity takes part in the S9-T32 provider choice: it is first in the user's order (agy, codex, cursor, claude), quota about to go to waste jumps ahead, and the harness falls back away from it like the others.

# Decisions (do not reopen)

1. `harness/provider_runner.py` builds the agy runner (S10-T4) when `agy.enabled`, next to the other runners.
2. Provider choice (`choose_provider`, `harness/routing.py`, rules of S9-T32) treats `agy` like the others, and this repo's `harness.yaml` sets `providers.priority: [agy, codex, cursor, claude]` with `agy.enabled: true`. The five S9-T32 scenarios in `tests/harness/test_routing_scenarios.py` must still pass with the real agy config and pool mapping. Its free capacity for a call is the headroom of the pool of the model the call would use (attempt n of the ladder, or the review/escalation model); if that pool is out but the ladder has an entry in the other pool with headroom, that entry counts. The reviewer is never the implementing provider, as for the others.
3. Out of quota, auth and launch failures disable agy for the task as for the other providers, and the per-task disable is cleared by `reset-task` and `run-task --fresh-budget` like the others.
4. Log the choice and its reason as for the other providers.
5. `harness doctor` reports whether `agy` is available when it is enabled (`agy --version`; never read credentials).
6. With `agy.enabled: false`, routing is exactly as before.

# Acceptance criteria

1. With fake headroom where agy's Gemini pool has the most urgent free capacity, an implementation call goes to agy with the Gemini model; with the Gemini pool at 0 and the 3p pool free, it uses the Claude ladder entry.
2. A review of an agy-implemented task never goes to agy.
3. A fake agy quota failure disables agy for the task and the next call goes to the next provider; `reset-task` clears it.
4. `doctor` shows the agy line only when enabled.

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
