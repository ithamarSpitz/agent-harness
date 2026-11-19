---
id: S16-T4
stage: 2
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S16-T1
allowed_paths:
  - harness/adapters/cursor.py
  - tests/harness/test_adapter_cursor.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Cursor adapter -->
# Goal

`CursorAdapter` in `harness/adapters/cursor.py` wraps `CursorAgentRunner` (`harness/runner.py`) behind the adapter protocol.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

`harness/adapters/base.py` (S16-T1) defines `ProviderAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault` and `classify_result`; read it and implement that protocol exactly. Each adapter receives its runner and config in its constructor and only delegates: the arguments it passes to the runner must be exactly the ones `ProviderAgentRunner` passes today at the line ranges named below, so moving the call into the adapter changes nothing. `plan_change` receives `model_class`: forward it as `model_class=` where the runner takes it today (agy, Copilot, Cursor); Claude's runner takes no model class, and Codex passes `model=config.codex.planner_model` instead. Plan-repair calls get no `on_process_start`.

Today in `harness/provider_runner.py`: implementation around lines 1087-1094 (`cursor_model_class = self._cursor_model_class(task)`, then `cursor.implement(..., model_class_override=cursor_model_class, ...)`, and afterwards, when that class is `auto` and the result is quota-exhausted, `self._remember_cursor_auto_exhaustion()`); review around lines 1308-1314; plan_change around lines 1434-1438. Cursor's budget is `self._cursor_implement_budget()` (around line 399), and Cursor consumes an attempt only when `_should_consume_cursor_attempt(result)` is true (around line 839). The Auto bookkeeping (`_cursor_auto_out_until`, `_remember_cursor_auto_exhaustion`, around lines 335-400) reads router state and stays in `provider_runner.py` this sprint.

# Decisions (do not reopen)

1. `name = "cursor"`. `enabled()` = `config.cursor.enabled`.
2. The constructor takes two callables from the router: `model_class_for(task) -> str` and `on_auto_quota() -> None`, plus `budget() -> int`. `implement(ctx)` calls `model_class_for(ctx.task)`, passes it as `model_class_override`, and calls `on_auto_quota()` when that class is `auto` and the result has `quota_exhausted`.
3. Add `consumes_attempt(result) -> bool` on this adapter with today's `_should_consume_cursor_attempt` rule. `BaseAdapter` does not get it in this task; S16-T9 reads it with `getattr(adapter, "consumes_attempt", None)`.
4. `find()` = `command_version(config.cursor.command)`.
5. `levels()`: worker = `config.cursor.models.get("worker")`, hard_worker = `.get("hard_worker")`, escalation = `.get("escalation")` (None when missing).
6. `implementation_budget()` = `budget()`.

# Acceptance criteria

1. `tests/harness/test_adapter_cursor.py` with a fake runner: implement passes the class from `model_class_for`; review and plan_change pass today's arguments.
2. An `auto` call with a quota result calls `on_auto_quota` once; a named-model call does not.
3. `consumes_attempt` is false for quota, waiting_cause and launch failures and true for a plain failure.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
