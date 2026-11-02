---
id: S8-T37
stage: 2
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/config.py
  - tests/harness/test_quota_patterns.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

Cursor's current out-of-usage message is recognized as quota, so the task waits instead of spending attempts.

# Background

On Nov 2 2026 Cursor answered `ActionRequiredError: Increase limits for faster responses You're out of usage. Switch to Auto, or ask your admin to increase your limit to continue.` None of the default `quota.exhaustion_patterns` in `harness/config.py` matched it, so each call was counted as a failed attempt (`Agent exit code 1`) and two tasks were BLOCKED in minutes.

# Decisions (do not reopen)

1. Add `"you're out of usage"`, `"you’re out of usage"` (typographic apostrophe) and `"increase limits for faster responses"` to the default `quota.exhaustion_patterns`. Keep every existing pattern.
2. Matching stays case-insensitive, as today; check how the existing matching works and do not change it.

# Acceptance criteria

1. The exact message above, as failed Cursor output, is classified as quota exhaustion with the default config.
2. A successful answer that quotes the message is not (existing rule: only failed calls are classified).
