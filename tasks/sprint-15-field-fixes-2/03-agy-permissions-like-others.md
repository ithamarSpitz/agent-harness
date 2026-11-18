---
id: S15-T3
stage: 1
model_class: worker
timeout_minutes: 35
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/agy_runner.py
  - harness/agy_permissions.py
  - tests/harness/test_agy_runner.py
  - tests/harness/test_agy_permissions.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- agy implements with full tool permission, like Claude Code and Copilot -->
# Goal

agy implementation calls no longer get commands silently denied: they run with agy's skip-permissions mode, the same trust level as Claude Code (`permission_mode: bypassPermissions`) and Copilot (`--allow-all-tools`). Review calls stay read-only.

# Background

Sprint S15 fixes what went wrong on 2026-11-18 in the harness's own S14 run and in BookNook's S16 run (harness 0.24.0). Evidence: `C:/Users/itas1/dev/harness-reports/2026-11-02-booknook-s3-bugs.md` items 20-22, and the S14 logs summarized in each brief. agy is turned off in this repo's `harness.yaml` until the agy tasks here are merged; do not turn it on.

User decision 2026-11-18. In S14-T10 attempt 1, agy produced nothing because a needed shell command was auto-denied: headless agy cannot ask, and `harness/agy_permissions.py` only emits exact-command allow rules (wildcards unsupported in agy 1.4.12+). agy's own error text: `Alternatively, re-run with --dangerously-skip-permissions to auto-approve all tools.` The command is built in `harness/agy_runner.py` around lines 150-190; the comment there (around line 155) forbids skip-permissions until a sandbox probe; that rule is replaced by this decision. The harness's own scope checks (allowed_paths, protected paths) run after every implementation regardless of provider.

# Decisions (do not reopen)

1. Implementation calls add `--dangerously-skip-permissions`. Review and plan-repair calls do not add it and keep their current read-only mode and settings.
2. Keep passing `--settings` with the existing rules (deny rules still help if agy honors them with skip); do not remove `agy_permissions.py` behavior.
3. Replace the comment around line 155 with the new rule and the date 2026-11-18, and say that S15-T3 replaced the sandbox-probe requirement by user decision.
4. No new config key.

# Acceptance criteria

1. `tests/harness/test_agy_runner.py`: an implementation command contains `--dangerously-skip-permissions` exactly once; review and plan-repair commands do not contain it.
2. Existing agy permission tests pass; the dropped-rule warning still fires for `command(graphify *)`.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy, Copilot), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Never change the user's agy, Codex, Cursor, Claude or Copilot configuration; per-call flags and environment only.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
