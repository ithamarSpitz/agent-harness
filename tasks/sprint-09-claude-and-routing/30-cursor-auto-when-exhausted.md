---
id: S9-T30
stage: 7
model_class: worker
timeout_minutes: 45
review: true
allow_protected: true
depends_on:
  - S9-T20
allowed_paths:
  - harness/config.py
  - harness/routing.py
  - harness/provider_runner.py
  - harness.yaml
  - harness/runner.py
  - tests/harness/test_cursor_auto_when_exhausted.py
  - tests/harness/test_provider_choice.py
  - tests/harness/test_auto_first.py
  - tests/harness/test_codex_provider.py
  - tests/harness/test_quota_fallback_expiry.py
  - tests/harness/test_retry_budget.py
  - tests/harness/test_launch_failure.py
  - tests/harness/test_launch_fallback_reason.py
  - tests/harness/test_cursor_runner.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Cursor uses its named models; Auto only replaces them while both Cursor pools are at 100% -->

# Goal

Cursor runs with its named models (as before the Auto trial). Only while both Cursor usage pools are at 100%, when the named models no longer work, does it run the `auto` model in their place, so Auto never spends the user's included Cursor usage.

# Background

Before Nov 2 2026 this repo's `harness.yaml` had named Cursor models: `worker: composer-2.5`, `hard_worker: cursor-grok-4.6-high`, `reviewer: cursor-grok-4.6-high`, `escalation: claude-opus-5-thinking-high`, with Codex first in `providers.priority`. When both Cursor pools hit 100%, the commit "TRIAL: Cursor models -> auto" mapped every class to `auto` (Auto ran on free credit then). The user's rule (Nov 4 2026): go back to the named models, and use Auto only while both pools ("Cursor models (Composer, Grok)" and "Other models") are at 100%. `harness/limits.py` `load_cursor` reads both (`auto_percent_used`, `api_percent_used`); `harness/headroom.py` exposes them as cursor windows `composer_grok` and `other_models`. S9-T5 decision 7 sends the first implementation attempt to Cursor Auto (`model_class == "auto"`).

# Decisions (do not reopen)

1. New `CursorConfig.auto_when_exhausted: bool = False` (`harness/config.py`).
2. In this repo's `harness.yaml`: `cursor.models` back to `worker: composer-2.5`, `hard_worker: cursor-grok-4.6-high`, `reviewer: cursor-grok-4.6-high`, `escalation: claude-opus-5-thinking-high`; set `auto_when_exhausted: true`; replace the Auto comments with one line describing this rule.
3. "Exhausted" means the latest headroom shows both cursor windows known and at 100% or more. Unknown, missing or stale data means not exhausted.
4. With `auto_when_exhausted` on and Cursor exhausted, an implementation call on Cursor runs model `auto` instead of the named model for its class. Being at 100% is then not "no capacity" for Cursor implementation.
5. Reviews never run on Auto (S9-T5): when Cursor is exhausted, Cursor is not available for review, and the review goes to another provider or waits as today.
6. The Auto-first rule (first implementation attempt goes to Cursor Auto; pass `model_class="auto"` to `choose_provider` for attempt 1 when the task's class resolves to the model `auto`) applies only when `auto_when_exhausted` is on and Cursor is exhausted. Otherwise the first attempt follows the normal choice (Codex first in this repo).
7. When Cursor is not exhausted, Cursor calls use the named models and the normal capacity rules, exactly as before the trial.
8. A real Cursor call that still reports a usage limit is handled as today (quota-disable for the task).
9. Log the choice: `cursor: auto (both pools at 100%)` or `cursor: <model>` in the existing routing log line.
10. With `auto_when_exhausted` off (the default), behavior is unchanged, so existing tests keep passing.
11. How Auto reaches Cursor (operator, Nov 4 2026, settles reviews of attempts 3 and 4): `cursor.models` stays exactly as decision 2 (no `auto` key, and `auto` is not a declarable task model class in validation). When the provider runner runs a Cursor implementation as Auto, it passes the model override `auto` to the Cursor runner, and `CursorAgentRunner` (`harness/runner.py`) sends `--model auto` for that override even though `auto` is not in `cursor.models`. A test in `tests/harness/test_cursor_runner.py` checks the real command line contains `--model auto` in that case and the named model otherwise.

# Acceptance criteria

Fake headroom, Codex and Cursor enabled, named models as in decision 2, `auto_when_exhausted: true`:
1. Cursor at 100%/100%: attempt-1 implement runs Cursor with model `auto`; a Cursor review is not chosen (another provider reviews, or it waits).
2. Cursor at 100%/40% or 40%/100%: never `auto`; Cursor follows the normal capacity rules (decision 7: a window at 100% means Cursor has no usage), so attempt 1 goes to Codex, and with Codex quota-disabled the implement goes to the next provider with usage or waits. Cursor usage unknown: attempt 1 goes to Codex; with Codex quota-disabled the implement runs on Cursor with `composer-2.5` (worker), never `auto`. These tests run with this repo's real setting `claude.enabled: true` (Claude in the fake headroom), not with Claude disabled. (Operator correction, Nov 4 2026: the earlier wording contradicted decision 7 and S9-T32 decision 2.)
3. Cursor at 40%/40%, Codex quota-disabled: review runs on Cursor with `cursor-grok-4.6-high`.
4. `auto_when_exhausted: false`: same inputs give today's results.
5. `harness.yaml` matches decision 2, and `harness validate` passes.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- Existing tests that check behavior you change on purpose are updated to the new behavior (only the ones listed in `allowed_paths`); never weaken a test to make it pass. If another existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
