---
id: S8-T39
stage: 3
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S8-T28
allowed_paths:
  - harness/provider_runner.py
  - tests/harness/test_quota_fallback_expiry.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

A task that moved to Cursor because Codex was out of quota goes back to Codex once Codex has quota again. It never waits on Cursor's quota while Codex is usable.

# Background

On Nov 2 2026 Codex ran out, so three S8 tasks fell back to Cursor (`_mark_codex_disabled` in `harness/provider_runner.py` stored `provider.codex.disabled = "quota:unspecified"` in the run meta). Then Cursor ran out too (monthly, resets in 24 days), so the tasks became `WAITING_QUOTA` on Cursor. When Codex refilled, `resume` probed only Cursor, failed in 9 seconds and slept 60 minutes, again and again: the disable flag never expires, so Codex was never tried. The operator had to clear the flags by hand.

# Decisions (do not reopen)

1. A Codex disable whose reason starts with `quota` is temporary. Disables for other reasons (`auth_or_cli_unavailable`, `integration_error:*`, model unavailable) stay as today.
2. When a task leaves a quota wait (any `WAITING_QUOTA` resume or probe), clear every temporary (quota) disable of that task, for both providers and all phases, before choosing the provider. Provider priority then picks Codex first again; if Codex is still out, the existing fallback to Cursor happens again (one short call).
3. Also clear them when a task is restarted by `retry`, `unblock`, `reset-task`, `rerun-blocker` or `run-task --fresh-budget`, if those paths go through `provider_runner.py`; if they do not, list it in OPEN_ISSUES instead of editing other files.
4. Log one line when a temporary disable is cleared: `codex re-enabled for <task> (was: <reason>)`.

# Acceptance criteria

1. With fakes: Codex reports quota, the task moves to Cursor, Cursor reports quota, the task waits. Then Codex succeeds: on resume the next implementation call goes to Codex.
2. A Codex disable with reason `auth_or_cli_unavailable` is not cleared by a quota resume.
3. Existing provider and quota tests pass.
