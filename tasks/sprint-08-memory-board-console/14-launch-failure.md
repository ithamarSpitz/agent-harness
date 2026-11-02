---
id: S8-T14
stage: 2
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S8-T16
  - S8-T25
  - S8-T26
allowed_paths:
  - harness/codex_runner.py
  - harness/runner.py
  - harness/scheduler.py
  - harness/models.py
  - tests/harness/test_launch_failure.py
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

<!-- Agent program cannot start -->
# Goal

When the Codex or Cursor program cannot be started at all (not installed, not executable, wrong path), the task stops at once with a clear reason, instead of waiting forever or spending attempts.

# Background

After S7-T5 the two providers disagree: a Codex launch `OSError` becomes `WAITING_PROVIDER_ERROR` (`harness/codex_runner.py`, the `except OSError` around `Popen`), so a missing Codex waits forever; a Cursor launch `OSError` (`harness/runner.py`) is an ordinary failed attempt, so it burns the retry budget on something retrying cannot fix.

# Decisions (do not reopen)

1. A launch failure (`OSError` from starting the process, for either provider) is a setup problem. Mark the result with a new flag `launch_failure=True` and the message `<provider> could not start: <error>`.
2. If another provider is configured and usable for that phase, fall back to it for this task (same mechanism as quota fallback) without spending an attempt.
3. Otherwise the task becomes `BLOCKED` immediately, without spending further attempts, with that message as its error, and `harness failure` shows it plus the hint `check the program with harness setup-machine`.
4. Network, busy, provider-error and timeout waits are unchanged.
5. A process that starts but dies at once with a Windows start-up resource error (exit code 3221225794 = 0xC0000142, or 3221225495 = 0xC0000017) is a resource problem, not a provider problem and not a task failure: no attempt is spent, the provider is not disabled, and the task goes back to the queue to be started again later (when memory allows, after S8-T6). After 5 such launches in a row for the same task it becomes `BLOCKED` with `<provider> could not start: the computer is out of resources (exit code ...)`.

# Note (Nov 2026)

The fallback mechanism lives in `harness/provider_runner.py`, which this task may not change. Reusing the existing auth-failure fallback hook from the runners is acceptable here; S8-T44 gives launch failures their own fallback reason later. S8-T44 also stops `ProviderAgentRunner` from consuming a Cursor provider attempt on launch and resource failures (that code is in `provider_runner.py`). So in this task: do not work around it by faking `waiting_cause` or other flags, and scheduler-level tests of the 5-requeue rule may use a runner without model routing. Reviewers should not fail this task for the Cursor provider-attempt consumption; S8-T44 owns it.

# Acceptance criteria

1. A fake Codex launch failure with Cursor available falls back to Cursor, attempt count unchanged.
2. A fake launch failure with no other provider makes the task `BLOCKED` at once with the message; no wait status, no extra attempts.
3. The same holds for Cursor.
4. Existing waiting-status tests pass unchanged, except the two launch-failure tests in `tests/harness/test_codex_provider.py` and `tests/harness/test_cursor_runner.py`, which assert the old launch behavior and must be updated to decisions 1 to 3.

# Rules for every S8 task

- Standard library only. Windows, Linux and macOS all work; tests never depend on the host OS (inject OS-specific readers, fake them in tests).
- Never call real agents, real GitHub or the network in tests.
- Statuses use the names from S7-T5 (`IMPLEMENTING`, `CHECKING`, `REVIEWING`, `PASSED`, `MERGING`, `DONE`, `RETRY`, `BLOCKED`, `WAITING_*`).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
