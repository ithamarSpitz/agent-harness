---
id: S15-T4
stage: 2
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S15-T3
allowed_paths:
  - harness/agy_runner.py
  - tests/harness/test_agy_runner.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- agy 503 'No capacity available' is a capacity wait, not a failed attempt -->
# Goal

When Google reports no server capacity for an agy model, the harness treats it like any provider capacity problem: it waits or falls back, and no attempt is spent.

# Background

Sprint S15 fixes what went wrong on 2026-11-18 in the harness's own S14 run and in BookNook's S16 run (harness 0.24.0). Evidence: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` items 20-22, and the S14 logs summarized in each brief. agy is turned off in this repo's `harness.yaml` until the agy tasks here are merged; do not turn it on.

S14-T10 attempt 2 log (2026-11-18): `{"event":"result","result":{"status":"ERROR","error":"API error (attempt 2): UNAVAILABLE (code 503): No capacity available for model gemini-3.8-flash-high on the server","retryable":false,...}}`. `AGY_CAPACITY_PATTERNS` (`harness/agy_runner.py` around line 38) only has `"model capacity exhausted"` and `"rate limit"`, so this counted as a plain failure. Classification is around lines 515-570. Capacity cannot be checked ahead of a call: `agy /usage` reports quota only.

# Decisions (do not reopen)

1. Add `"no capacity available"`, `"unavailable (code 503)"` and `"resource_exhausted"` to `AGY_CAPACITY_PATTERNS` (matching stays lowercase).
2. Matching uses the result event's `error` text as well as the response and diagnostics.
3. A quota pattern still wins over capacity when both match.

# Acceptance criteria

1. `tests/harness/test_agy_runner.py`: the exact JSON line above as the last stream event gives `capacity_exhausted=True`, `waiting_cause == "provider_busy"`, `ok=False`, not `quota_exhausted`.
2. A stream with both a quota message and the 503 text gives `quota_exhausted=True` and `capacity_exhausted=False`.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
