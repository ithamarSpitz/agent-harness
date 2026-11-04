---
id: S10-T3
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/agy_permissions.py
  - tests/harness/test_agy_permissions.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Shell permissions for headless agy calls -->
# Goal

Every agy call gets exactly the shell permissions the task needs, because headless agy cannot ask and silently denies everything else.

# Decisions (do not reopen)

1. New `harness/agy_permissions.py`: `build_settings(task_verification: list[str], base_allow: list[str], extra_allow: list[str] = []) -> str` returns the JSON for `--settings`: `{"permissions":{"allow":[...]}}`, where each verification command becomes `command(<command>)`, plus `base_allow` and `extra_allow`, without duplicates and in a stable order.
2. A verification command containing shell operators (`&&`, `|`, `;`) is split into its parts, each allowed separately.
3. Review calls get only `base_allow` (reviewers do not run the project's checks).
4. Find out from the installed agy (`agy --help`, `agy help`, the changelog, or one real call in a temporary folder) whether permission rules accept `*` wildcards, and state the evidence in your report. If they do not, `base_allow` entries with `*` are expanded to the exact commands the harness prompts mention, and say so in OPEN_ISSUES.
5. Settled (operator, Nov 4 2026, from the reviews of attempts 1 and 2, which tested the installed agy 1.4.12): agy permission rules match exact commands only; `*` wildcards and `regex:` rules are not supported and must not be emitted. Rules with arguments that are known when the call is built are made exact per call: `build_settings` takes an optional `base_sha` and then adds `command(git diff <base_sha>)`, `command(git diff <base_sha> --stat)` and `command(git diff --stat <base_sha>)`; `git status`, `git diff`, `git diff --stat` and `git log --oneline -20` are allowed exactly. Rules whose arguments cannot be known (any `graphify ...` command, anything else ending in `*` in `base_allow`) are dropped, with one warning log line naming them; agy calls run without the code map (S10-T4 tells agy in its prompt that the code map is not available; reviewers still get callers of changed symbols in the prompt from S9-T16). Tests check the generated JSON contains no `*` and no `regex:` rule.

# Acceptance criteria

1. A task with verification `python -m pytest -q tests/harness` and `npm test && npm run lint` gives the expected allow list, as valid JSON.
2. Review settings contain only `base_allow`.
3. Duplicates are removed and order is stable.

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
