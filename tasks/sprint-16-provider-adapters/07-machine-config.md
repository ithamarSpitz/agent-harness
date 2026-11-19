---
id: S16-T7
stage: 1
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/config.py
  - tests/harness/test_config_machine.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- One machine config file, overridden by the project's harness.yaml -->
# Goal

Provider settings can live once per machine in `<harness home>/config.yaml`; a project's `harness.yaml` overrides only what it names.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

Design doc, "Config per machine": one file in the harness home folder lists the providers, models per level, caps and key variable names; today each project repeats everything. `harness_home()` (`harness/home.py`) is `HARNESS_HOME` or `~/.harness`. `HarnessConfig.load(path)` (`harness/config.py` around line 686) reads only the project file. Every command loads config through it (`cli.py` line 94, `limits.py` lines 115 and 154).

# Decisions (do not reopen)

1. `HarnessConfig.load(path)` first reads `harness_home() / "config.yaml"` when it exists, then the project file, and deep-merges: mappings merge key by key, the project wins; lists and scalars from the project replace the machine value.
2. A machine file that is not a mapping, or is invalid YAML, raises `ValueError` naming the file. A missing machine file changes nothing.
3. Add `load(path, *, machine_path: Path | None = None)` so tests pass their own path; default is the harness home file.
4. The harness never creates or edits the machine file.
5. Validation that runs today on the merged values runs the same way (for example the Copilot auto/effort check from S15-T1).

# Acceptance criteria

1. `tests/harness/test_config_machine.py`: a machine file setting `copilot.enabled: true` and `codex.review_model: x` with a project file setting only `codex.review_model: y` gives copilot enabled and review model `y`.
2. A project list (for example `codex.implementation_sequence`) replaces the machine list entirely.
3. No machine file: the config equals today's result for the same project file.
4. A machine file containing `- a` (a list) raises ValueError naming the file.
5. Tests set `HARNESS_HOME` or `machine_path` to a temp folder; none read the real home folder.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
