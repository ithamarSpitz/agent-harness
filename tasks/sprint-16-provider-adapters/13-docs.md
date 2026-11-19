---
id: S16-T13
stage: 9
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S16-T1
  - S16-T2
  - S16-T3
  - S16-T4
  - S16-T5
  - S16-T6
  - S16-T7
  - S16-T8
  - S16-T9
  - S16-T10
  - S16-T11
  - S16-T12
  - S16-T14
  - S16-T15
  - S16-T16
allowed_paths:
  - README.md
  - CHANGELOG.md
  - SPEC.md
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Docs for provider adapters, the machine config and the new lint -->
# Goal

README, CHANGELOG and SPEC describe the adapters and the machine config file as merged.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

# Decisions (do not reopen)

1. CHANGELOG: an "Unreleased" entry, one line per merged S16 task.
2. README: a short "Providers" section: the five adapter parts, where adapters live (`harness/adapters/`), and the machine file `<harness home>/config.yaml` with the merge rule (mappings merge, lists and scalars replace, project wins), with a three-line example that holds no secrets; and the `test-not-allowed` brief-lint warning in the brief-writing section.
3. SPEC.md: update only sentences that the merged code now contradicts.
4. Describe only what the code does; if code and this brief disagree, follow the code and report it. Code gaps are not fixed here; report them.

# Acceptance criteria

1. Every command, path and setting named in the new text exists in the code.
2. Nothing outside these three files changes.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
