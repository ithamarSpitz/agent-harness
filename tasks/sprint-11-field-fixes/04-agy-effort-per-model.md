---
id: S11-T4
stage: 1
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/config.py
  - harness/agy_runner.py
  - tests/harness/test_config_agy.py
  - tests/harness/test_agy_runner.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- agy: --effort only for models that accept it (report item 12) -->
# Goal

agy calls never fail because `--effort` was passed to a model that does not accept it.

# Background

BookNook, 0.20.0, checked by hand: `agy -p ... --model claude-sonnet-4-6 --effort high` fails in about 3 s with `invalid model selection ... --effort is not supported for model "claude-sonnet-4-6"`; the same for `claude-opus-4-6-thinking`. Gemini models accept it. The S10-T1 defaults (`harness/config.py` `AgyConfig`) are `review_model: claude-sonnet-4-6` with `review_effort: high`, and a third implementation entry `claude-opus-4-6-thinking` with `effort: high`, so every agy review failed. `review_effort` and `escalation_effort` cannot be empty, so a project cannot omit the flag.

# Decisions (do not reopen)

1. New config list `agy.effort_models` (default `["gemini-"]`): model-name prefixes that accept `--effort`. `AgyAgentRunner._command` (`harness/agy_runner.py`) adds `--effort` only when an effort is set AND the model starts with one of these prefixes; otherwise it omits the flag and logs one debug line. No model names are hard-coded in the runner.
2. `review_effort`, `escalation_effort` and a ladder entry's `effort` may be `null` (omitted); validation still rejects unknown effort values.
3. Keep the default models (the user chose them); with decision 1 they now work.

# Acceptance criteria

1. Command-line tests: Claude review model with `review_effort: high` gives no `--effort`; a Gemini model with `effort: high` gives `--effort high`; `effort_models: []` gives no `--effort` for any model.
2. Config tests: `review_effort: null` loads; `review_effort: extreme` is rejected; `effort_models` must be a list of strings.

# Rules for every S11 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes and recorded samples. A task whose brief says so may run the real `agy` a few times to check its behavior (`/usage` costs no tokens; prefer Gemini Flash for anything else).
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor or Claude configuration; per-call settings only.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Source of these bugs: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` (item numbers below), found on the BookNook project with harness 0.20.0.
- Existing tests that check behavior you change on purpose are updated to the new behavior (they are in allowed_paths); never weaken a test to hide a real regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
