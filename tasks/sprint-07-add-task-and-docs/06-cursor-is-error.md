---
id: S7-T6
stage: 4
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S7-T5
allowed_paths:
  - harness/runner.py
  - tests/harness/test_cursor_runner.py
context: []
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

A Cursor stream-json answer that reports an error gets the right waiting status even when the Cursor process exits with code 0.

# Background

Cursor's final `result` event can carry `is_error: true` while the process exits 0. Today (`harness/runner.py`, around the result parsing and `_waiting_cause`) that answer is treated as a success.

# Decisions (do not reopen)

1. When the final result event has `is_error: true`, classify its text with the same rules as a failed call: network → `WAITING_NETWORK`, overload → `WAITING_PROVIDER_BUSY`, 5xx/server error → `WAITING_PROVIDER_ERROR`, quota → `WAITING_QUOTA`; anything else is an ordinary failed attempt.
2. When `is_error` is false or missing and the exit code is 0, the answer is a success no matter what words it contains (no pattern matching on successful answers).

# Review findings from the previous round (address them)

- `test_cursor_successful_answer_does_not_trigger_wait_pattern` must use success texts that really match the configured patterns, so it fails if successful answers were classified: include at least one text for each list (for example "Implemented WebSocket reconnect handling", "quota exceeded", "503 service unavailable", "server overloaded"). Keep the `is_error` false / missing parametrization.

# Acceptance criteria

1. Recorded-style fake stream-json outputs with exit code 0 and `is_error: true` for each cause produce the matching status.
2. A successful answer containing words like "network", "timeout" or "503" stays a success.
3. Existing Cursor runner tests pass.

Rules: change only `allowed_paths`; tests use fakes, never real agents, GitHub or the network; statuses use the S7-T5 names.
