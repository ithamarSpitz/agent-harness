---
id: S8-T45
stage: 3
model_class: worker
timeout_minutes: 25
review: true
allow_protected: false
depends_on:
  - S8-T18
allowed_paths:
  - harness/quota.py
  - tests/harness/test_quota_wait.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

The quota wait uses the time a provider is really usable again: for one provider, the latest reset among its full windows; across providers, the earliest of those.

# Background

Found by the operator after S8-T18's review (Nov 2026). `QuotaManager.next_wait_seconds` (`harness/quota.py`) collects one reset per Codex window at 100 % and takes the earliest of all. When both the Codex 5-hour and weekly windows are full, the harness wakes at the 5-hour reset, Codex is still blocked by the weekly window, and the wait message named the wrong time.

# Decisions (do not reopen)

1. Group the resets by provider; a provider's usable-again time is the latest reset among its windows at 100 %.
2. The wait is until the earliest usable-again time across providers that are out, plus the existing grace; all other S8-T18 rules (minimum 60 s, at most 24 h per sleep, fallback interval, message format) stay.

# Acceptance criteria

1. Fake Codex with 5-hour full (resets in 2 h) and weekly full (resets in 3 days): wait is 24 h (the per-sleep cap) and the message names the weekly reset time.
2. Codex 5-hour full in 2 h (weekly not full) and Cursor out in 5 days: wait is 2 h + 60 s.
3. Existing S8-T18 tests pass.
