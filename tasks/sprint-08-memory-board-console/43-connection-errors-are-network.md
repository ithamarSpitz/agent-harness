---
id: S8-T43
stage: 4
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S8-T38
allowed_paths:
  - harness/config.py
  - tests/harness/test_network_patterns.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

Connection errors from the provider CLIs are recognized as network waits, not as failed agent calls.

# Background

On Nov 2 2026 a Cursor review call ended after 4 seconds with `Error: [unavailable] connect ECONNREFUSED 192.168.16.1:443`. None of the default network patterns (`connection failed`, `connection lost`, `connection reset`, `network error`, `transport error`, `websocket`) matched it, so it was treated as a failed review and an attempt was spent, although the network came back a minute later. The operator added the patterns below to the harness repo's own `harness.yaml` as a stopgap.

# Decisions (do not reopen)

1. Add these to the default `network_patterns` of both the capacity config and the Codex config in `harness/config.py` (matching is case-insensitive as today): `econnrefused`, `econnreset`, `etimedout`, `enotfound`, `eai_again`, `getaddrinfo`, `[unavailable]`.
2. Keep all existing defaults. Do not add bare `unavailable` (too broad; it would catch model-unavailable errors).

# Acceptance criteria

1. The Nov 2 message above is classified as a network wait for Cursor, and the same text is a network wait for Codex.
2. `model unavailable` text is still classified as model unavailable, not network.
3. Existing tests pass.
