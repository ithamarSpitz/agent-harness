---
id: S13-T2
stage: 2
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on:
  - S13-T1
allowed_paths:
  - harness/validation.py
  - tests/harness/test_validation.py
  - tests/harness/test_plan_change_revalidation.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- harness validate <sprint> reports brief findings -->
# Goal

`harness validate <SPRINT>` (and every run, which validates its sprint first) shows the brief checks from S13-T1.

# Background

Sprint S13 builds a brief writer so later sprints get fewer bad briefs: free checks in `harness validate` (T1, T2), a cheap read-through by a model (T4, T5), and a skill that writes briefs (T3). Design doc approved 2026-11-07.

`ProjectValidator.validate` in `harness/validation.py` already reads each task file's frontmatter (around line 125). `harness/cli.py` calls `validate(sprint_id)` for `validate` and before a run; a run stops only on errors.

# Decisions (do not reopen)

1. Only when `validate` gets a `sprint_id`, run `lint_brief(task, text, self.root, self.config.paths.protected)` from `harness/brief_lint.py` on each task's brief text. Without a sprint id, change nothing (old sprints must not flood plain `harness validate`).
2. An `error` finding goes to `report.errors`, a `warning` to `report.warnings`, both as `f"{task.id}: brief {code}: {message}"`.
3. A brief that cannot be read is already an error today; do not lint it.

# Acceptance criteria

1. Test in `tests/harness/test_validation.py`: a sprint with a task listing a protected path and `allow_protected: false` fails validation with a `brief protected` error; with `allow_protected: true` it passes.
2. Test: a brief without the stop rule gives a `brief stop-rule` warning and `report.ok` stays true.
3. Test: `validate()` without a sprint id gives no `brief` findings for the same project.
4. Existing tests in both test files pass; update fixtures only if they now hit a new error, and say which in your report.

# Rules

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code, agy), real GitHub, or the network. Use fakes.
- Never read, print, log or store tokens or credentials.
- Existing tests that check behavior you change on purpose are updated (they are in allowed_paths); never weaken a test to hide a regression.
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
- If an existing test outside `allowed_paths` fails, stop and report it in OPEN_ISSUES (STATUS: blocked).
