---
id: S15-T6
stage: 3
model_class: hard_worker
timeout_minutes: 60
review: true
allow_protected: false
depends_on:
  - S15-T2
  - S15-T7
  - S15-T8
allowed_paths:
  - harness/scheduler.py
  - harness/board.py
  - tests/harness/test_scheduler_integration.py
  - tests/harness/test_board.py
  - tests/harness/test_attempt_count.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Attempts count only real tries; the board shows the real limit -->
# Goal

A task's attempt number goes up only for a try that really used the implementer, and the board's "try N/M" never shows N above M.

# Background

Sprint S15 fixes what went wrong on 2026-11-18 in the harness's own S14 run and in BookNook's S16 run (harness 0.24.0). Evidence: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` items 20-22, and the S14 logs summarized in each brief. agy is turned off in this repo's `harness.yaml` until the agy tasks here are merged; do not turn it on.

S14-T10 showed `try 9/3` on 2026-11-18. `Scheduler` increments `runtime.attempt` at the top of every implementation loop pass (`harness/scheduler.py` around line 1546), before knowing the outcome. Passes that end in a capacity wait, a transient error, a provider fall back (`launch_failure`, `config_fault` from S15-T2, provider unavailable), or a killed run that is resumed all added one. The loop guard is `runtime.attempt < max_attempts` plus `has_implementation_budget`. The board row prints `try {task.attempt}/{task.max_attempts}` (`harness/board.py` around line 185); `max_attempts` there is not the limit the scheduler uses with provider routing (`ProviderAgentRunner.max_implementation_attempts`).

# Decisions (do not reopen)

1. After `self.runner.implement(...)` returns, if the result is a capacity wait, transient error, `launch_failure`, `config_fault` or `quota_exhausted`, give the attempt back (`runtime.attempt -= 1`) before saving. Real tries (ok, failed work, verification or review failure) keep it.
2. On resume, a task found in IMPLEMENTING with no recorded result for its current attempt reuses that attempt number instead of adding one.
3. The board's M is the same limit the scheduler enforces for that task (with routing: the runner's `max_implementation_attempts(task)`), and N is shown as `min(N, M)`.
4. Retry budgets per provider (`provider_attempts`) are unchanged.

# Acceptance criteria

1. New `tests/harness/test_attempt_count.py` with a fake runner: capacity, transient, launch_failure and config_fault results leave `runtime.attempt` unchanged; a failed verification adds one.
2. A task stopped mid-IMPLEMENTING and resumed keeps its attempt number.
3. `tests/harness/test_board.py`: the row never shows N greater than M; M equals the routing limit for a routed task.
4. Existing scheduler and board tests pass, updated only where they asserted the old counting.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
