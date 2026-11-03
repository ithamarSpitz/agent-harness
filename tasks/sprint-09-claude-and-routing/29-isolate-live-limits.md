---
id: S9-T29
stage: 4
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S9-T10
allowed_paths:
  - tests/conftest.py
  - tests/harness/test_live_limits_isolated.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Tests never read this machine's real Codex, Claude or Cursor usage -->

# Goal

The test suite gives the same result whatever this machine's real provider usage is.

# Background

`ProviderAgentRunner` (`harness/provider_runner.py`) defaults its headroom reader to `harness.headroom.headroom_snapshot`, which reads live usage through `LimitsPaths.from_env()` (`harness/limits.py`: `CODEX_HOME` or `~/.codex`, `CLAUDE_HOME` or `~/.claude`, and Cursor's `state.vscdb` from `default_cursor_state_db()`). On Nov 3 2026, right after S9-T10 merged, two tests passed while the real Codex 5-hour window was at 100% and failed once it reset (real Codex showed capacity, so the harness cleared the test's Codex quota disable):
- `tests/harness/test_codex_provider.py::test_codex_quota_fallback_is_per_task_and_next_task_probes_codex_again`
- `tests/harness/test_quota_fallback_expiry.py::test_quota_disable_cleared_on_waiting_quota_resume_tries_codex_again`
They fail on main now and block every task's verification. Tests that read real machine state already happened before (free memory, fixed by `isolate_default_free_memory`; harness home, fixed by `isolate_harness_home`).

# Decisions (do not reopen)

1. Add an autouse fixture to `tests/conftest.py`, next to the existing isolation fixtures, that points `CODEX_HOME` and `CLAUDE_HOME` at fresh empty temp folders and monkeypatches `harness.limits.default_cursor_state_db` to return a missing file under a temp folder. With those, live headroom has no data and the order is `providers.priority` (S9-T10 decision 5).
2. Tests that set these variables or inject a headroom reader themselves keep working (their own monkeypatch runs after the autouse one).
3. Do not change `harness/` code or the two tests above; they are correct once the environment is isolated.

# Acceptance criteria

1. New `tests/harness/test_live_limits_isolated.py`: inside a test, `LimitsPaths.from_env()` points under temp folders, not under `Path.home()`, and `harness.headroom.headroom_snapshot(max_age_seconds=0)` reports no known usage for Codex.
2. The two tests above pass on this machine whatever its real Codex usage.
3. The full suite passes.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
