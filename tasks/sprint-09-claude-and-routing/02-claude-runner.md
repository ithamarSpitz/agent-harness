---
id: S9-T2
stage: 2
model_class: hard_worker
timeout_minutes: 55
review: true
allow_protected: false
depends_on:
  - S9-T1
allowed_paths:
  - harness/claude_runner.py
  - tests/harness/test_claude_runner.py
  - tests/harness/fixtures/claude-stream-sample.jsonl
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Run Claude Code as an agent -->
# Goal

The harness can run a task's implementation, retry or review with Claude Code, the same way it runs Codex and Cursor.

# Decisions (do not reopen)

1. New `harness/claude_runner.py` with a runner class that has the same public interface and `AgentResult` fields as the Cursor runner (`harness/runner.py`) and the Codex runner (`harness/codex_runner.py`). Reuse their shared helpers (logging, process launch, watchdog, usage recording) instead of copying them.
2. Command: `claude -p --output-format stream-json --verbose --model <model> --effort <effort> --permission-mode <permission_mode>`, run with the task worktree as the working directory; the prompt goes on stdin (long prompts, Windows).
3. Parse the stream-json events: collect the final `result` event's text as the agent answer, its `is_error` flag, and its `usage` (input, output, cache read, cache write tokens) into the usage record. Count tool calls; count code-map calls the same way the Cursor runner does (shell commands that run `graphify`).
4. Stall watchdog: same rule as Cursor (last activity = latest of CLI output and worktree file activity), same `stall_timeout_minutes`.
5. Model choice: implementation attempt n uses entry n of `implementation_sequence` (last entry for later attempts); review uses `review_model` / `review_effort`; model class `escalation` uses the escalation pair.
6. Record a short sample of real stream-json output: run `claude -p --output-format stream-json --verbose --model haiku "say ok"` once in a temporary empty folder, save it as the fixture, and build the parser tests on it. If that run is not possible in your environment, write the fixture from the documented event format and say so in OPEN_ISSUES.

# Acceptance criteria

1. The built command line matches decision 2 for implementation attempts 1-3, review and escalation.
2. Parsing the fixture gives the answer text, token counts and `ok`.
3. A fake process with `is_error: true` gives a failed result with its text.
4. A silent fake process with no file activity stalls; one that writes files does not.

# Context

The user has three $20 subscriptions: ChatGPT Plus (Codex: 5-hour and weekly windows), Cursor ($20 monthly; its own models Composer/Grok in one pool, other models in another), and Claude Pro (Claude Code: 5-hour and weekly windows). The user's own Claude Code session controls the harness and shares the same Claude limits, so harness agents must leave Claude capacity for it. Today the harness has two providers (Codex first, Cursor as per-task fallback). S9 adds Claude Code as a third provider and chooses the provider per call from live usage.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
