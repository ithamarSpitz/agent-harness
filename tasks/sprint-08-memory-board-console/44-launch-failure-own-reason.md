---
id: S8-T44
stage: 4
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S8-T14
  - S8-T39
allowed_paths:
  - harness/provider_runner.py
  - harness/codex_runner.py
  - harness/runner.py
  - tests/harness/test_launch_fallback_reason.py
  - tests/harness/test_codex_provider.py
  - tests/harness/test_cursor_runner.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

A provider that cannot start is reported and handled as a launch failure, not as an authentication failure.

# Background

S8-T14 made launch failures (`OSError` when starting the CLI) fall back to the other provider. Because it could not change `harness/provider_runner.py`, it set `auth_failure=True` on the result to reuse the existing auth fallback path. So usage events and reports count launch failures as auth failures, and the provider is disabled for the task with reason `auth_or_cli_unavailable`.

# Decisions (do not reopen)

1. Runners set only `launch_failure=True` for launch failures, not `auth_failure`.
2. `provider_runner.py` falls back on `launch_failure` the same way it does on auth failure, with the disable reason `launch_failure`, and records the usage event with `launch_failure: true`.
3. Launch failures and Windows resource launch failures (`resource_launch_failure`) never consume a provider attempt in `ProviderAgentRunner` for either provider (found by the S8-T14 reviewer: `implement` calls `_consume_provider_attempt(task, "cursor")` whenever `waiting_cause is None`, so after one Cursor resource failure the budget is gone and the task cannot be requeued 5 times). Test it through `ProviderAgentRunner` with `manages_model_routing` true, not with a fake scheduler runner.
4. Everything else S8-T14 decided stays (no attempt spent, BLOCKED with its message when no other provider is usable, resource exit codes requeue).

# Acceptance criteria

1. A fake Codex launch failure with Cursor usable falls back to Cursor; the usage event has `launch_failure` true and `auth_failure` false; the disable reason is `launch_failure`.
2. A real auth failure still uses reason `auth_or_cli_unavailable`.
3. Five Cursor resource launch failures in a row through the real `ProviderAgentRunner` routing requeue the task each time without spending a provider attempt, then BLOCK with the resource message.
4. Existing tests pass.
