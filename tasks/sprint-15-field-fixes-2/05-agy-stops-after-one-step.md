---
id: S15-T5
stage: 3
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S15-T4
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

<!-- agy 'success' with no report is a provider fault that falls back -->
# Goal

An agy call that ends "successfully" without the report the harness asked for is treated as an agy fault: the harness falls back to another provider for that task and no attempt is spent.

# Background

Sprint S15 fixes what went wrong on 2026-11-18 in the harness's own S14 run and in BookNook's S16 run (harness 0.24.0). Evidence: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` items 20-22, and the S14 logs summarized in each brief. agy is turned off in this repo's `harness.yaml` until the agy tasks here are merged; do not turn it on.

Seen twice in S14-T10 (2026-11-18), in an implementation (attempt 3) and a review: agy returned `{"status":"SUCCESS","response":"I'll start by reading the required context files...","num_turns":1}` and stopped. The implementation prompt asks for a final report with `STATUS`, `CHANGED_FILES`, `TEST_RESULTS`, `OPEN_ISSUES` (`harness/prompts.py` around line 179); reviews must contain a `VERDICT:` line. Reviews already fall back ("review fault on agy ... reviewing again elsewhere"); implementations counted as a normal failed attempt. `AgentResult.launch_failure` makes `ProviderAgentRunner` fall back without consuming an attempt (`harness/provider_runner.py` around line 1116).

# Decisions (do not reopen)

1. In `AgyAgentRunner`, an implementation result with status SUCCESS whose response has no line starting with `STATUS` (case-insensitive, after optional `#`, `*` or spaces) returns `ok=False`, `launch_failure=True`, `error=f"agy ended after {num_turns} turn(s) without a STATUS report"`.
2. A review result with status SUCCESS and no `VERDICT:` line returns the same kind of result with `error=f"agy ended after {num_turns} turn(s) without a VERDICT"`.
3. The raw response's last 300 characters go to the task log (not the error) to help find the cause later.
4. Results that contain the expected report are unchanged.

# Acceptance criteria

1. `tests/harness/test_agy_runner.py`: the recorded one-turn SUCCESS line above, for implement, gives `launch_failure=True`, `ok=False` and the error text with `1 turn(s)`.
2. The same for review gives the VERDICT error.
3. An implementation response ending with a `STATUS: done` report and a review with `VERDICT: PASS` are `ok=True`.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
