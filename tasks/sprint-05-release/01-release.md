---
id: S5-T1
stage: 1
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/**
  - tests/harness/**
  - README.md
context:
  - readme
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

When a sprint finishes successfully, the harness runs the project's release commands by itself as the sprint's last step. For the harness's own repository that means: push, then install the new version, so every project uses it at once and nobody installs anything by hand. A sprint that failed or stopped never releases.

# Decisions (do not reopen)

1. **Config.** A new `release` section, a `ReleaseConfig` dataclass: `commands: []` (shell commands run from the repository root, in order) and `timeout_minutes: 15`. Empty `commands` means the step does nothing. A missing section keeps today's behavior.
2. **When.** Exactly once per sprint, right after the last stage barrier passes and the sprint is marked complete, in the integration checkout. Not after a single task, not after `--dry-run`, not when any stage failed, was rolled back, or the sprint stopped for quota, capacity or a blocked task. `resume` of a sprint that then completes runs it once.
3. **Execution.** Run the commands one by one with the same environment handling and process cleanup the harness uses for verification commands, each with `timeout_minutes`. Stop at the first failing command. Write all output to `.harness/logs/<sprint>-release.log`.
4. **Failure does not undo anything.** A failed release never rolls back merged code and never marks tasks failed. The sprint stays complete; `status <sprint>` shows `release: FAILED (<command>, exit <code>)` and the rerun command `harness release <sprint>`. On success `status` shows `release: done <time>`. Store this in the sprint's state.
5. **Manual command.** `harness release <sprint>` runs the release commands for a completed sprint (refuses with a clear message when the sprint is not complete). It is how a failed release is retried.
6. **README.** A short "Release after a sprint" section with two examples: a project that deploys on push (`git push origin HEAD`) and the harness itself (`git push origin HEAD` then `python -m pip install --user --no-deps --force-reinstall .`).

# Out of scope

Version bumping, tags, changelogs, GitHub releases, any per-task release, changing how stages merge or roll back.

# Acceptance criteria

Tests with fake commands (small Python scripts that record their calls) prove:

1. A completed sprint runs the commands once, in order, from the repository root; the log file contains their output.
2. A sprint with a failed stage, a blocked task or a quota stop runs nothing; `--dry-run` runs nothing.
3. A failing command stops the rest, keeps the merged code and the sprint's complete status, and `status` shows the failure and `harness release <sprint>`.
4. `harness release <sprint>` reruns them on a completed sprint and refuses on an incomplete one.
5. No `release` section: behavior unchanged.
