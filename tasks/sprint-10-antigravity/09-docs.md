---
id: S10-T9
stage: 5
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: 
  - S10-T1
  - S10-T2
  - S10-T3
  - S10-T4
  - S10-T5
  - S10-T6
  - S10-T7
  - S10-T8
allowed_paths:
  - docs/spec/providers.md
  - docs/spec/configuration.md
  - docs/spec/operations.md
  - SPEC.md
  - README.md
  - CHANGELOG.md
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Docs for S10 -->
# Goal

The spec parts, README and changelog describe S10 exactly as implemented.

# Scope

1. `docs/spec/providers.md`: agy as a provider, its ladder and pools, permissions and `denied_actions`, quota/capacity/auth handling, reviews in plan mode, UI checks.
2. `docs/spec/configuration.md`: the `agy:` section and every key.
3. `docs/spec/operations.md`: `harness limits` and the headroom snapshot for agy.
4. `README.md`: enabling agy in a project (install and sign in are the user's; the harness only runs `agy`). Under 250 lines.
5. `CHANGELOG.md`: one S10 entry. `SPEC.md`: only the index lines that point to changed parts.

# Acceptance criteria

Every statement matches the code; nothing unrelated changes.

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
