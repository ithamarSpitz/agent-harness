---
id: S16-T2
stage: 2
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S16-T1
allowed_paths:
  - harness/adapters/codex.py
  - tests/harness/test_adapter_codex.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Codex adapter -->
# Goal

`CodexAdapter` in `harness/adapters/codex.py` wraps `CodexAgentRunner` behind the adapter protocol.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

`harness/adapters/base.py` (S16-T1) defines `ProviderAdapter`, `CallContext`, `FindResult`, `Levels`, `Fault` and `classify_result`; read it and implement that protocol exactly. Each adapter receives its runner and config in its constructor and only delegates: the arguments it passes to the runner must be exactly the ones `ProviderAgentRunner` passes today at the line ranges named below, so moving the call into the adapter changes nothing. `plan_change` receives `model_class`: forward it as `model_class=` where the runner takes it today (agy, Copilot, Cursor); Claude's runner takes no model class, and Codex passes `model=config.codex.planner_model` instead. Plan-repair calls get no `on_process_start`.

Today in `harness/provider_runner.py`: implementation around lines 1062-1080 (availability check, then `self.config.codex.implementation_profile(index)` with `index = provider_attempt`, then `codex.implement(..., model=, reasoning_effort=, env=, code_map_offered=, on_process_start=)`); review around lines 1287-1300 (`model=self.config.codex.review_model`); plan_change around lines 1416-1430; enabled check `_codex_enabled_for` (around line 212) uses `self.config.codex.enabled`. Budget is `len(self.config.codex.implementation_sequence)`. `CodexAgentRunner.availability()` (`codex_runner.py` line 99) checks `--version` and `login status`.

# Decisions (do not reopen)

1. `name = "codex"`. `enabled()` = `config.codex.enabled`.
2. `implement(ctx)`: when `availability()` is false, return `AgentResult(False, "", "Codex CLI/auth unavailable", provider="codex", auth_failure=True, fallback_reason="auth_or_cli_unavailable")` without calling the runner; otherwise call `implement` with the profile for `ctx.provider_attempt`, as today.
3. `review` and `plan_change` do the same availability check and return the same result shape on failure (with `fallback_reason="auth_or_cli_unavailable"`).
4. `find()` uses `availability(refresh=refresh)`: installed and signed in when true; on false, `installed` is decided by `command_version(config.codex.command)`.
5. `levels()`: worker = model of profile 0, hard_worker = model of profile index `min(2, len-1)`, escalation = model of the last profile.
6. `implementation_budget()` = `len(config.codex.implementation_sequence)`.

# Acceptance criteria

1. `tests/harness/test_adapter_codex.py` with a fake runner: `implement` passes the same model/effort per `provider_attempt` as `implementation_profile`, and the other keyword arguments unchanged.
2. Unavailable Codex returns the auth result with `fallback_reason="auth_or_cli_unavailable"` and never calls the runner, for implement, review and plan_change.
3. `levels()` on the default config returns the default models.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
