---
id: S15-T1
stage: 1
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/config.py
  - harness/copilot_runner.py
  - tests/harness/test_config_copilot.py
  - tests/harness/test_copilot_runner.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Copilot: the model auto is never combined with a reasoning effort -->
# Goal

No Copilot call the harness builds combines `--model auto` with `--reasoning-effort`, and a config that asks for it is rejected when it loads.

# Background

Sprint S15 fixes what went wrong on 2026-11-18 in the harness's own S14 run and in BookNook's S16 run (harness 0.24.0). Evidence: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` items 20-22, and the S14 logs summarized in each brief. agy is turned off in this repo's `harness.yaml` until the agy tasks here are merged; do not turn it on.

BookNook item 21: the default `CopilotConfig.implementation_sequence` (`harness/config.py` around line 207) ends with `{model: auto, auto_tier: intelligence, effort: high}`, and `escalation_effort` defaults to `"high"` with `escalation_model: "auto"` (around line 221). Copilot CLI 1.0.107 rejects that at once with `Error: Model "auto" does not support reasoning effort configuration (requested: "high")`. `CopilotAgentRunner` adds `--reasoning-effort` whenever an effort is set (`harness/copilot_runner.py` around line 235). Config loading checks each profile in `_load_copilot` / `validate_profile` (`harness/config.py` around lines 571-660); `harness validate` and `harness doctor` both load the config, so a load error reaches both.

# Decisions (do not reopen)

1. Defaults: `implementation_sequence` = `[{model: auto, auto_tier: balance}, {model: auto, auto_tier: intelligence}, {model: auto, auto_tier: intelligence}]`; `escalation_effort` = `None`. Other defaults unchanged.
2. Loading a profile (implementation step, review or escalation) whose model is `auto` and whose effort is not null raises `ValueError` naming the key, for example `copilot.implementation_sequence[2]: model auto does not take an effort; remove effort or name a concrete model`.
3. `CopilotAgentRunner` still never passes `--reasoning-effort` with model `auto` (defensive check next to the existing one around line 235), even if a profile slipped through.
4. A concrete model with an effort stays allowed and keeps passing `--reasoning-effort`.

# Acceptance criteria

1. `tests/harness/test_config_copilot.py`: the defaults have no effort on any auto step and `escalation_effort is None`; loading `{model: auto, effort: high}` as a step, as review and as escalation each raise with the key in the message; `{model: gpt-5.4, effort: high}` loads.
2. `tests/harness/test_copilot_runner.py`: the built command for an auto profile with an effort contains no `--reasoning-effort`; for a concrete model with effort it does.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
