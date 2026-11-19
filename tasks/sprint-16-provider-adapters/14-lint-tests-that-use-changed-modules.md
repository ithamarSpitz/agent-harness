---
id: S16-T14
stage: 1
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/brief_lint.py
  - tests/harness/test_brief_lint.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Brief lint warns about tests that import a changed module but are not allowed -->
# Goal

`lint_brief` warns, at no token cost, when a test file imports a module the brief changes but the test file is not in `allowed_paths`.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

Repeated waste: on 2026-11-19 S15-T5 ran three tries and then blocked because 16 tests in `tests/harness/test_agy_quota.py` used fake agy output that the change made invalid, and that file was not in `allowed_paths`. The same kind of miss happened in earlier sprints. `harness/brief_lint.py` (`lint_brief`, around line 132) already emits findings such as `named-not-allowed` and `allowed-missing`; `harness validate` and `harness add-task` print them.

# Decisions (do not reopen)

1. New finding code `test-not-allowed`, level WARN. For each `harness/<name>.py` in `allowed_paths`, scan `tests/**/*.py` under the repo root for `import harness.<name>`, `from harness.<name> import`, `from harness import <name>` or `from .<name>`-style relative imports of that module; each matching test file that is not in `allowed_paths` gives one finding: `tests/... imports harness.<name> (changed by this brief) but is not in allowed_paths`.
2. Text scan with a regex, no import or execution of test files. Skip when the repo root has no `tests` folder.
3. Cap at 10 findings per brief, then one line `... and N more`.

# Acceptance criteria

1. `tests/harness/test_brief_lint.py`: a temp repo with `harness/foo.py`, `tests/test_a.py` (imports `harness.foo`) and `tests/test_b.py` (does not) and a brief allowing only `harness/foo.py` gives exactly one `test-not-allowed` finding, for `test_a.py`.
2. Adding `tests/test_a.py` to allowed_paths removes it.
3. Existing brief-lint tests pass.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
