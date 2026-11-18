---
id: S15-T2
stage: 2
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S15-T1
allowed_paths:
  - harness/agent_runner.py
  - harness/copilot_runner.py
  - harness/provider_runner.py
  - tests/harness/test_copilot_runner.py
  - tests/harness/test_copilot_wiring.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Copilot: a bad-flag error skips that step instead of costing an attempt -->
# Goal

When Copilot CLI exits on a flag or option error before doing any model work, the harness moves to Copilot's next implementation step: no task attempt is used and Copilot is not turned off for the task.

# Background

Sprint S15 fixes what went wrong on 2026-11-18 in the harness's own S14 run and in BookNook's S16 run (harness 0.24.0). Evidence: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` items 20-22, and the S14 logs summarized in each brief. agy is turned off in this repo's `harness.yaml` until the agy tasks here are merged; do not turn it on.

BookNook item 21 follow-up: the auto+effort error happened twice in S16 (T4, then T2). Each time the failed step turned Copilot off for the whole task; with Codex and Claude out of quota the task sat in `WAITING_QUOTA (none)` for about 1.5 hours and needed an operator `reset-task`. `CopilotAgentRunner` classifies output with pattern tuples (`harness/copilot_runner.py` around lines 29-75 and 495-560). `ProviderAgentRunner._implement_with_routing` (`harness/provider_runner.py` around line 1000) disables a provider via `_disable_provider` when `_provider_unavailable(result)` or `_is_launch_failure(result)` (around line 1116), and otherwise calls `_consume_provider_attempt`. `AgentResult` is in `harness/agent_runner.py` around line 11.

# Decisions (do not reopen)

1. Add `config_fault: bool = False` to `AgentResult`.
2. New constant `COPILOT_CONFIG_ERROR_PATTERNS` in `copilot_runner.py` with at least `"does not support reasoning effort"`, `"unknown option"`, `"error: option"`, `"unexpected argument"` (lowercase match like the others). A failed call matching it, and no quota/auth/model-unavailable pattern, returns `config_fault=True` with `error` = the first matching output line.
3. In `_implement_with_routing`, a `config_fault` result advances that provider's step (`_consume_provider_attempt`), does not disable the provider, and immediately tries the same provider again if it has steps left; when none are left it falls through to the next provider as for an exhausted sequence. The loop must not spin: each config fault consumes exactly one step.
4. A `config_fault` is logged once per task and step as a WARNING naming the step index and the error line.
5. Review and plan-repair calls with a config fault fall back to the next provider for that call without disabling Copilot for the task.

# Acceptance criteria

1. `tests/harness/test_copilot_runner.py`: the real 1.0.107 message `Error: Model "auto" does not support reasoning effort configuration (requested: "high")` with exit 1 gives `config_fault=True`, `ok=False`, not `launch_failure`, not `model_unavailable`.
2. `tests/harness/test_copilot_wiring.py` with fake runners: step 1 config fault, step 2 succeeds gives success on Copilot, Copilot not disabled (`provider.copilot.disabled` meta unset), and the provider attempt count is 2.
3. All steps config faults: the call falls through to the next enabled provider; Copilot is not marked disabled.
4. A review config fault on Copilot is reviewed by another provider; Copilot stays enabled for implementation.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
