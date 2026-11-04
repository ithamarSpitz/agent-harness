---
id: S10-T4
stage: 2
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on: 
  - S10-T1
  - S10-T3
allowed_paths:
  - harness/agy_runner.py
  - tests/harness/test_agy_runner.py
  - tests/harness/fixtures/agy-stream-sample.jsonl
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Run coding agents with agy -->
# Goal

The harness can run an implementation or review agent with agy and turn its output into the same `AgentResult` the other providers return.

# Decisions (do not reopen)

1. New `harness/agy_runner.py` with a runner class that has the same public interface and `AgentResult` fields as the Cursor, Codex and Claude runners (`harness/runner.py`, `harness/codex_runner.py`, `harness/claude_runner.py`). Reuse their shared helpers (logging, process launch, watchdog, usage recording) instead of copying them.
2. Command: `agy -p --input-format stream-json --output-format stream-json --model <model> [--effort <effort>] --mode accept-edits --settings <json from S10-T3>` with the task worktree as the working directory. The prompt is written to stdin as one stream-json user message, then stdin is closed (long prompts do not fit on the Windows command line). Find the exact message shape with one real tiny call (`gemini-3.8-flash-low`, prompt "reply ok", temporary empty folder), save its stdout as the fixture, and build the parser tests on it. If that is not possible, use `-p "<prompt>"` with `--output-format json` and stdin closed, and say so in OPEN_ISSUES.
3. Review calls use `--mode plan` (read-only) with the review model and the review settings from S10-T3.
4. Result: the final result event's `response` is the answer; `status != "SUCCESS"` or a non-zero exit is a failure. A non-empty `denied_actions` makes the call a failure whose error lists the denied actions (so a silently blocked check is never taken as success). Usage (input, output, thinking, cache read tokens) goes into the usage record with `provider: "agy"`, the model and the pool id (S10-T1 `pools`).
5. Stall watchdog: same rule as the other runners (last activity = latest of CLI output and worktree file activity), same `stall_timeout_minutes`.
6. Model choice: implementation attempt n uses entry n of `implementation_sequence` (last entry for later attempts); review uses `review_model`/`review_effort`; model class `escalation` uses the escalation pair. A ladder entry whose pool is reported exhausted for this task (S10-T5) is skipped in favor of the next entry in another pool.
7. Each call logs `agy --version` output once per run (agy updates itself, so logs show which version ran).

# Acceptance criteria

1. The built command lines match decisions 2 and 3 for attempts 1-3, review and escalation.
2. Parsing the fixture gives the answer text, token counts and `ok`.
3. A fake result with `denied_actions` fails with the actions listed; `status` other than SUCCESS fails.
4. A silent fake process with no file activity stalls; one that writes files does not.

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
