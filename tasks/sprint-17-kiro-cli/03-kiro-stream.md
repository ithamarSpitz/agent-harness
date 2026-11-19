---
id: S17-T3
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/kiro_stream.py
  - tests/harness/test_kiro_stream.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Parse and classify Kiro stream-json output -->
# Goal

A pure module `harness/kiro_stream.py` turns Kiro's stream-json stdout into a result (final text, status, error, credits, denied tools) and fault flags, with no process handling.

# Background

Sprint S17 adds Kiro CLI (`kiro-cli`, version 3.1.0, installed at `%LOCALAPPDATA%/Kiro-Cli/kiro-cli.exe`, on PATH) as the first CLI written the new way after S16 (design doc "Harness Redesign", section "Providers as adapters"). S16 added `harness/adapters/` (`base.py`: `BaseAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault`, `command_version`; `AdapterRegistry` in `__init__.py`) and one adapter per CLI that wraps its runner. The closest existing CLI is GitHub Copilot: `harness/copilot_runner.py` (`CopilotAgentRunner`), `harness/copilot_usage.py`, `harness/adapters/copilot.py`, `CopilotConfig` in `harness/config.py`. Copy their structure; do not refactor them. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S17 tasks exist when it runs.

Facts recorded live on 2026-11-19 (do not re-probe; tests use these as samples):
- Headless call: `kiro-cli chat --no-interactive --output-format stream-json --model <id> --trust-workspace [--trust-all-tools | --trust-tools=fs_read,execute_bash]`, prompt on stdin, cwd = workspace. stdout is JSON Lines; a denied tool also prints a plain non-JSON line `[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.`
- Models (`kiro-cli chat --list-models --format json`): auto, claude-sonnet-4.5, claude-haiku-4.5, deepseek-3.2, minimax-m2.5, minimax-m2.1, glm-5, qwen3-coder-next. The models report `"reasoning":{"support":"unavailable"}`, so `--effort` is never passed.
- Plan: KIRO FREE, 50 credits per month. `/usage` costs no credits.

Recorded stream (success), one JSON object per line:
```
{"type":"runStarted","data":{"payloadSchema":"acp","acpProtocolVersion":1,"engine":"v2"}}
{"type":"metadata","data":{"sessionId":"888118b0","contextUsagePercentage":3.55,"reasoning":{"support":"unavailable","effortLevels":[]}}}
{"type":"sessionUpdate","data":{"sessionId":"888118b0","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"PONG"}}}}
{"type":"sessionUpdate","data":{"sessionId":"888118b0","update":{"sessionUpdate":"tool_call","toolCallId":"t1","title":"Running: git status --short","kind":"execute","rawInput":{"command":"git status --short"}}}}
[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.
{"type":"sessionUpdate","data":{"sessionId":"888118b0","update":{"sessionUpdate":"tool_call_update","toolCallId":"t1","kind":"execute","status":"failed","title":"shell"}}}
{"type":"metadata","data":{"sessionId":"888118b0","contextUsagePercentage":6.17,"meteringUsage":[{"value":0.0158,"unit":"credit","unitPlural":"credits"}],"turnDurationMs":1381}}
{"type":"runFinished","data":{"sessionId":"888118b0","status":"success","stopReason":"end_turn","finalText":"PONG","finalTextTruncated":false}}
```
Recorded stream (unknown model, exit code 1):
```
{"type":"runStarted","data":{"payloadSchema":"acp","acpProtocolVersion":1,"engine":"v2"}}
{"type":"runError","data":{"sessionId":"e409e0f3","stage":"prompt","message":"Internal error (code -32603): Encountered an error in the response stream: The model 'no-such-model' is not available. Please use '/model' to select a different model and try again. (request_id: 381e63b3)"}}
```

# Decisions (do not reopen)

1. `@dataclass(slots=True) class KiroStream: final_text: str; status: str | None; stop_reason: str | None; error: str | None; credits: float; denied_tools: int; finished: bool`.
2. `parse_kiro_stream(text: str) -> KiroStream`: one line at a time. Lines starting with `[denied]` add 1 to `denied_tools`. Other non-JSON lines are ignored. `runFinished` sets `finished=True`, `status`, `stop_reason`, and `final_text` from `finalText`; when `finalTextTruncated` is true or `finalText` is missing, `final_text` is the joined `agent_message_chunk` texts instead. `runError` sets `error` to its `message` (the last one wins). `credits` is the sum of every `meteringUsage[].value` whose `unit` is `credit`. With no `runFinished`, `final_text` is the joined chunks.
3. `@dataclass(slots=True) class KiroFlags: quota: bool; auth: bool; model_unavailable: bool; capacity: bool; transient: bool; config: bool` and `classify_kiro(stream: KiroStream, returncode: int, raw_output: str) -> KiroFlags`. All false when `returncode == 0` and `status == "success"` and no `error`. Otherwise match case-insensitively against `stream.error` plus the raw output, in this precedence (first match wins, only one flag true):
   - quota: `out of credits`, `credit limit`, `insufficient credits`, `usage limit`, `monthly limit`, `quota`, `limit reached`, `upgrade your plan`;
   - auth: `not logged in`, `kiro-cli login`, `please log in`, `login required`, `unauthorized`, `401`, `token expired`, `session expired`;
   - model_unavailable: the text contains `the model '` and `is not available`;
   - config: `unexpected argument`, `unknown option`, `invalid value`;
   - capacity: `throttl`, `too many requests`, `429`, `rate limit`, `capacity`;
   - transient: `service unavailable`, `503`, `502`, `internal server error`, `timed out`, `connection`, `network`.
   A failure that matches nothing has all flags false (a task failure).

# Acceptance criteria

1. The success sample gives `final_text == "PONG"`, `status == "success"`, `credits == 0.0158`, `denied_tools == 1`, `finished`, and all flags false with return code 0.
2. The unknown-model sample gives `error` containing `is not available`, `finished == False`, and only `model_unavailable` with return code 1.
3. A `runFinished` with `finalTextTruncated: true` returns the joined chunk text.
4. One test per flag list in decision 3 (one pattern each), plus a precedence test: a message with both `quota` and `429` sets only `quota`.
5. Garbage and empty input do not raise.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes and the recorded samples in this brief.
- Never read, print, log or store tokens, credentials or the signed-in account's e-mail. Never change the user's Kiro, agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Kiro stays off by default (`kiro.enabled: false`); nothing changes for a project that does not enable it.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
