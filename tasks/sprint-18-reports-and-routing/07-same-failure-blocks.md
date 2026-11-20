---
id: S18-T7
stage: 3
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on:
  - S18-T2
  - S18-T4
  - S18-T6
allowed_paths:
  - harness/verifier.py
  - harness/scheduler.py
  - harness/provider_runner.py
  - tests/harness/test_same_verification_failure.py
  - tests/harness/test_budget_vs_wait.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Same verification failure from two providers blocks; used-up budgets are not a quota wait -->
# Goal

A verification command that fails the same way no matter who writes the code blocks the task at once with the real reason, and a task whose providers used up their attempt budgets never shows as a quota wait with provider `none`.

# Background

Sprint S18 (harness 0.27.0 -> 0.28) fixes four field bugs and changes routing to the user's rules:
- Kiro smoke test (2026-11-20): Kiro answers `**STATUS**: COMPLETED`; `kiro_runner._has_report` only accepts a plain `STATUS:` line, so every Kiro implementation counts as a missing report.
- The learned memory estimate per agent (`resources.memory_per_agent_gb`) only goes up (6.3 GB after one big peak), so only one agent runs at a time.
- BookNook S24-T2 (2026-11-20): the verification command `node_modules/.bin/deno check scripts/refresh-all.ts` fails on Windows because the harness runs commands through `cmd.exe`, which cannot start a program path written with `/`. Codex and Claude each spent their attempts on the same failure; then the task showed `WAITING_QUOTA ... none usage quota exhausted during implement`, a wrong label: no quota was out, the attempt budgets were.
- Routing (user rules, design answers Q05, R02, R03): among providers that can take the call, the one whose quota resets soonest goes first; after 2 failed tries the next provider by quota takes over at a smarter level.

`harness/verifier.py` builds `VerificationResult(ok, summary, failures, changed_files)` and adds `Verification command failed: <command>` per failed command (around line 292). `harness/scheduler.py` around line 1320 sets RETRY on a failed verification. `harness/provider_runner.py` `_implement_with_routing` (around line 984) returns `AgentResult(False, "", choice.reason, provider="none", quota_exhausted=True)` when no provider is left, which `_pause_for_quota` (scheduler, around line 1108) turns into `none usage quota exhausted during implement`.

# Decisions (do not reopen)

1. `VerificationResult` gets `failure_signature: str | None`: for the first failed command, the command text plus its last 3 non-empty output lines with every digit run replaced by `0`; `None` when verification passed or the failure was a timeout.
2. The scheduler stores the signature and the implementing provider per attempt in task meta. When two attempts in a row by two different providers fail with the same signature, it blocks the task: `Verification fails the same way for <p1> and <p2>: <command> -> <last line>. The command or the environment is likely wrong, not the code; fix the brief's verification.`
3. In `_implement_with_routing`, when the choice is `None` and at least one provider was excluded only for a used-up attempt budget, the result is not a quota result. If some other provider is out of quota with a reset time, the task waits for that provider: `waiting_provider` is that provider and the message is `every provider with usage used its attempts; waiting for <provider> until <time>`. If none has a reset time, the task is BLOCKED with `every provider used its attempts` plus the last verification failure.
4. A real quota wait (all candidates out of quota, no budget used up) is unchanged.

# Acceptance criteria

1. `tests/harness/test_same_verification_failure.py`: Codex then Claude failing with output `'node_modules' is not recognized as an internal or external command` blocks after the second attempt with the decision-2 message; two failures whose last lines differ (different failing test names) retry as today; two same-signature failures by the same provider retry as today.
2. Tests: budgets used up while Copilot is out until a known time gives a wait on `copilot` with the decision-3 message, never `none`; budgets used up with no reset time blocks.
3. Existing tests in `tests/harness/test_budget_vs_wait.py` pass, updated only where they encoded the `none` quota wait.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot, Kiro), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
