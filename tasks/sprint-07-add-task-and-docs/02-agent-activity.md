---
id: S7-T2
stage: 2
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S7-T1
allowed_paths:
  - harness/runner.py
  - harness/codex_runner.py
  - harness/config.py
  - harness/usage.py
  - harness/init_project.py
  - tests/harness/test_cursor_runner.py
  - tests/harness/test_agent_activity.py
context: []
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

For every agent call, Codex and Cursor alike, record what the agent ran (in particular how many code-map commands) and how many tokens it used, so the harness can later compare token use with and without the code map. What the agent sees and costs does not change.

# Facts

- **Codex** already runs with `--json`. Its log holds every event; `CodexRunner._parse_events` extracts the final agent message and token usage. Shell commands appear as `{"type": "item.completed", "item": {"type": "command_execution", "command": "<full command line, often wrapped in a shell>", ...}}`.
- **Cursor** runs with `--output-format text` today (`cursor.output_format`), so its log holds only the final answer and there are no token numbers. The CLI also supports `stream-json`. A real recording is in `tests/harness/fixtures/cursor-stream-sample.jsonl`. Its events, one JSON object per line:
  - `{"type": "system", "subtype": "init", ...}`, `{"type": "user", ...}`
  - `{"type": "thinking", "subtype": "delta" | "completed", "text": ...}`
  - `{"type": "tool_call", "subtype": "started" | "completed", "tool_call": {"shellToolCall": {"args": {"command": "python --version", ...}, ...}}}`
  - `{"type": "assistant", "message": {"content": [{"type": "text", "text": "DONE"}]}}`
  - last: `{"type": "result", "subtype": "success", "is_error": false, "result": "DONE", "usage": {"inputTokens": 7995, "outputTokens": 67, "cacheReadTokens": 15872, "cacheWriteTokens": 0}}`
- The output format only changes what the CLI prints locally; it does not change the prompt or the model's work.

# Decisions (do not reopen)

1. **Cursor default becomes `stream-json`.** Change the `cursor.output_format` default and the `harness init` template to `stream-json`; `text` still works for projects that set it.
2. **Cursor parsing.** Write the full event stream to the attempt's log file. The final answer is the `result` event's `result` text; if there is no `result` event, use the last `assistant` message text; if neither exists, use the raw output (as with `text`). Unknown event types are ignored. Read tokens from the `result` event's `usage`.
3. **Only the final answer travels.** Review verdict parsing, quota/capacity pattern matching on the answer, `last_error`, `review_feedback`, failure summaries and every text passed into a later prompt use the extracted final answer, never raw events. (Codex already works this way; keep it.) Quota/capacity detection must still see Cursor's error output, so also match those patterns against non-JSON lines and `result` events with `is_error: true`.
4. **Usage records, both providers.** Each record gets `code_map_calls: <n>`: shell commands whose command line contains the configured `code_map.command` as a word (Codex `command_execution` items; Cursor `shellToolCall` commands). Cursor records also get `input_tokens`, `output_tokens`, `cached_input_tokens` (from `cacheReadTokens`) in the same field names Codex records use.
5. Stall and timeout detection keep working: stream output counts as activity.

# Acceptance criteria

1. Using the fixture, the Cursor final answer is `DONE`, tokens are recorded, and `code_map_calls` is 0; a variant fixture with a `graphify explain ...` shell call gives 1.
2. A Codex log with a wrapped `graphify path ...` command gives `code_map_calls` 1.
3. A Cursor reviewer stream whose final answer starts with `VERDICT: FAIL` produces review feedback equal to the final answer only (no JSON in `review_feedback`, `last_error` or the next attempt's prompt).
4. A Cursor quota error still pauses for quota with `stream-json`.
5. With `output_format: text`, behavior is unchanged.
