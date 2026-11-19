---
id: S16-T15
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/state.py
  - tests/harness/test_state.py
  - tests/harness/test_state_save.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Saving state.json retries when Windows denies the replace -->
# Goal

A run no longer stops when another process is reading `.harness/state.json` at the moment the scheduler saves it.

# Background

Sprint S16 starts the approved harness redesign (design doc "Harness Redesign", section "Providers as adapters"): each CLI becomes one adapter with the same five parts (Find, Quota, Run, Classify, Levels), so the routing loops stop naming providers. Today `harness/provider_runner.py` (`ProviderAgentRunner`) holds one `self.<provider>` runner per CLI (codex, cursor, claude, agy, copilot) and branches on the provider name in `_choose`, `_has_other_candidate`, `max_implementation_attempts`, `has_implementation_budget`, `_implement_with_routing`, `_review_with_routing`, `_plan_change_with_routing` and `brief_check`. The runners themselves (`codex_runner.py`, `runner.py` = Cursor, `claude_runner.py`, `agy_runner.py`, `copilot_runner.py`) are not rewritten; adapters wrap them. Kiro CLI is added in S17 as the first CLI written the new way, so the adapter shape must not assume the five current CLIs. Each task starts only after the tasks in its `depends_on` are merged, so modules this brief says come from earlier S16 tasks exist when it runs.

Field bug 2026-11-19 04:51: the S15 run stopped with `Run stopped: [WinError 5] Access is denied: '.harness/state.tmp' -> '.harness/state.json'` while a status tab ran `harness status` every 20 seconds. `StateStore.save` (`harness/state.py` around line 30) writes `state.tmp` and calls `tmp.replace(self.path)` once; on Windows the replace fails with `PermissionError` while any reader has the target open.

# Decisions (do not reopen)

1. `StateStore.__init__(self, path, *, replace_tries: int = 20, sleep: Callable[[float], None] = time.sleep)`. `save` retries the replace on `PermissionError` up to `replace_tries` times with a 0.1 s sleep between tries (about 2 s), then raises the last error as today.
2. Tests pass `sleep` as a recorder; no real sleeping in tests.
3. Readers (`load`) retry `PermissionError` and `json.JSONDecodeError` the same way, so `harness status` does not crash on a save in progress.
4. No change to the file format or path.

# Acceptance criteria

1. `tests/harness/test_state_save.py`: a replace that raises `PermissionError` twice then succeeds saves the state, with 2 sleeps recorded.
2. A replace that always raises gives `PermissionError` after the configured tries.
3. `load` survives one `PermissionError` and one truncated JSON read, then returns the saved data.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Behavior stays the same unless a decision below says otherwise: this sprint moves code, it does not change routing, quotas, prompts or command lines.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
