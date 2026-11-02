---
id: S8-T35
stage: 3
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on:
  - S8-T14
  - S8-T26
allowed_paths:
  - harness/runner.py
  - tests/harness/test_cursor_stream_answer.py
  - tests/harness/fixtures/cursor-review-stream.jsonl
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

With `cursor.output_format: stream-json`, a Cursor reviewer's `VERDICT: PASS` is recognized.

# Background

Found on Nov 2 2026 (S8-T2): the reviewer answered PASS, but the task went back to implementation. Cursor's stream-json `result` event joins all assistant messages of the call with no separator, so the answer read `...against the acceptance criteria.VERDICT: PASS`. The scheduler accepts the verdict only on a line of its own (`line.strip() == "VERDICT: PASS"` in `harness/scheduler.py`), so every stream-json Cursor review fails. The harness repository went back to `output_format: text` until this is fixed.

# Decisions (do not reopen)

1. In the stream-json parser of `harness/runner.py`: when the `"".join(...)` of the collected assistant message texts equals the `result` text, the answer is the assistant messages joined with `"\n"` (keeping message boundaries). Otherwise keep today's rule (the `result` text, else the last assistant message).
2. Do not loosen the verdict rule in the scheduler.
3. Build the test fixture from the real shape: several `assistant` events (narration messages, then a final message starting with `VERDICT: PASS`) and a `result` event whose `result` is their plain concatenation.

# Acceptance criteria

1. The fixture gives an answer in which `VERDICT: PASS` is a line of its own.
2. A stream whose `result` differs from the joined messages keeps the `result` text.
3. Existing Cursor runner tests pass.
