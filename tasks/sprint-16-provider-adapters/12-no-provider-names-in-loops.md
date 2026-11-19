---
id: S16-T12
stage: 8
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S16-T16
  - S16-T7
allowed_paths:
  - tests/harness/test_adapter_boundaries.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- A test keeps provider names out of the routing loops -->
# Goal

A test fails if a provider name is compared by string again inside the router's dispatch methods.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

After S16-T8 to T11 and T16, `_choose`, `_has_other_candidate`, `has_implementation_budget`, `max_implementation_attempts`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check` in `harness/provider_runner.py` no longer branch on provider names.

# Decisions (do not reopen)

1. New `tests/harness/test_adapter_boundaries.py` parses `harness/provider_runner.py` with `ast`, finds those eight methods on `ProviderAgentRunner`, and fails if any contains a comparison (`==`, `!=`, `in`) against a string constant equal to `codex`, `cursor`, `claude`, `agy` or `copilot`, or an attribute access `self.codex`, `self.cursor`, `self.claude`, `self.agy`, `self.copilot`.
2. The failure message names the method and line.
3. If a method still breaks the rule when you run it, do not edit `provider_runner.py`: report the method and line in OPEN_ISSUES and mark the test for that method with `pytest.mark.xfail(strict=True, reason=...)`.

# Acceptance criteria

1. The test passes on the merged code (or xfails strictly with a reported reason).
2. Adding `if provider == "codex": pass` to `_implement_with_routing` in a temporary copy makes the check fail (test the checker on a source string, not on the real file).

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
