---
id: S16-T17
stage: 8
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S16-T7
allowed_paths:
  - harness/limits.py
  - tests/harness/test_limits.py
  - tests/harness/test_agy_limits.py
  - tests/harness/test_copilot_limits.py
  - tests/harness/test_live_limits_isolated.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- harness limits shows Antigravity and Copilot outside a project -->
# Goal

`harness limits` run from any folder shows Antigravity and Copilot: their quota when enabled, or one "off" line saying where to enable them, never silently nothing.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

Field report 2026-11-19: the user ran `harness limits` from their home folder and saw only Codex, Cursor and Claude. `_agy_settings` and `_copilot_settings` in `harness/limits.py` read only `harness.yaml` in the current folder; with no such file both providers count as disabled and `render_limits` drops their blocks without a word. S16-T7 added the machine config `<harness home>/config.yaml` (`harness.home.harness_home()`), merged under the project file by `HarnessConfig.load(path, *, machine_path=None)`.

# Decisions (do not reopen)

1. When the project file (`config_path`, or `harness.yaml` in `root` / the current folder) exists, behavior is as today (`HarnessConfig.load` already merges the machine file under it).
2. When the project file does not exist and `harness_home() / "config.yaml"` exists, `_agy_settings` and `_copilot_settings` read the machine file alone with the same parsing and validation as `HarnessConfig.load` (for example `HarnessConfig.load(machine_file, machine_path=<a path that does not exist>)`, or an equivalent helper in `limits.py`). Load errors behave as today (agy logs its one warning; both count as off).
3. When neither file exists, both stay off as today.
4. Text output: when Antigravity is off, print a block `Antigravity  off (set agy.enabled: true in harness.yaml or <machine config path>)`; when Copilot is off, `Copilot  off (set copilot.enabled: true in harness.yaml or <machine config path>)`. The path is the real `harness_home() / "config.yaml"`. Order of blocks is unchanged: Codex, Cursor, Claude, Antigravity, Copilot.
5. JSON output (`--json`) is unchanged: disabled providers stay absent.
6. No quota command or network call is made for a disabled provider.
7. Tests point `harness_home()` at a temp folder (monkeypatch `HARNESS_HOME` or the function) so the real machine config is never read.

# Acceptance criteria

1. No project file, machine file with `agy.enabled: true` and `copilot.enabled: true`: text output contains the Antigravity and Copilot quota blocks from fake sources.
2. No project file, no machine file: text output contains both "off" lines naming the machine config path; the fake quota sources are not called.
3. Project file with `agy.enabled: false` and machine file with `agy.enabled: true`: Antigravity shows "off" (project wins).
4. `--json` with both off: no `agy` or `copilot` keys, as today.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
