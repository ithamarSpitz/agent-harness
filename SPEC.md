# Agent harness specification

This document is the entry point for how the harness behaves today, as implemented under `harness/`. Detailed behavior lives in linked parts; this file does not duplicate them.

## Purpose

The harness orchestrates coding agents (Cursor CLI and optionally OpenAI Codex CLI) against any Git project. Work is expressed as a **roadmap of task briefs** with mechanical verification and optional independent review. Each task runs in an isolated **Git worktree**; successful task commits merge into the **integration checkout** (the branch you run from) only after every active task in a **stage** reaches `PASSED`. Stages are **atomic**: merge or stage-verification failure rolls the integration checkout back to the pre-stage checkpoint. Agents do not commit or merge; the harness does.

Core ideas: plan as data (`tasks/roadmap.yaml` and briefs), parallel tasks within a stage when `allowed_paths` do not conflict, retry and **provider-specific** routing (Codex sequences vs Cursor model classes) with fallback where implemented, and human-gated **plan repair** when blocked work indicates a plan gap.

## Concepts

| Term | Meaning |
| --- | --- |
| **Project** | Git repository root the harness runs from (`--root`, default `.`). |
| **Roadmap** | YAML file (default `tasks/roadmap.yaml`): sprints, task ids, brief paths, per-sprint `stage_verification`. |
| **Sprint** | Named unit (`SprintSpec`) of tasks and optional stage-level verification. |
| **Stage** | Integer barrier. The lowest open stage runs (up to `max_parallel_agents`), then merges together. |
| **Task** | One brief (`TaskSpec`): scope, verification, model class, dependencies, etc. |
| **Brief / frontmatter** | Markdown with YAML frontmatter parsed for scheduling and prompts. Roadmap rows merge with brief frontmatter (roadmap wins on overlap when loading from roadmap). |
| **Worktree** | Per-task directory under `.harness/worktrees/<task-id>/` on branch `agent/<task-id>`, from the stage base commit. |
| **Integration checkout** | Main repo at `--root` on a named branch (not detached HEAD). Stage merges land here. |
| **Runtime directory** | `.harness/` (gitignored): state, logs, queue, worktrees, code-map build, failures, manifests, usage, plan revisions. |

State persists in `.harness/state.json`. Logs live under `.harness/logs/`.

## Parts

| Part | Contents |
| --- | --- |
| [Task lifecycle and stage barrier](docs/spec/lifecycle.md) | Statuses, transitions, retries, integration barrier, recovery commands. |
| [Providers, quota, and capacity](docs/spec/providers.md) | Provider priority, Codex vs Cursor rules, waits and fallback. |
| [Review and code map](docs/spec/review.md) | Independent review, UI check, optional Graphify code map. |
| [Operations](docs/spec/operations.md) | Post-sprint release, enqueueing tasks during a run. |
| [Configuration](docs/spec/configuration.md) | `harness.yaml` keys and task brief frontmatter. |

## Guarantees

- The integration checkout must be clean before a stage starts and before atomic rollback; stage failure restores the checkpoint with `git reset --hard` and `git clean -fd`.
- Implementation and review agents run in task worktrees. `harness plan-change propose` uses the task worktree when it exists, otherwise the integration checkout. The harness commits with message `harness: <task-id>` after verification and review when required.
- Verification rejects changes outside `allowed_paths` and, unless `allow_protected`, any match under `paths.protected`.
- A stage merges only after all its active tasks reach `PASSED` (no blocked tasks and no quota/capacity waits in the way of merge pickup). Merge runs stage verification on the integration checkout and rolls back on failure.
- Quota and capacity pauses preserve task/worktree state; implement-phase waits that decrement `attempt` on pause do not consume retry budget the way a failed implement call does.
- Failed **`harness release`** records failure in state metadata and does **not** undo merged sprint commits.
- An open plan-change request blocks `harness run` / `harness resume` until applied or rejected.
- Sprint integration branch and initial `base_ref` / `initial_base_commit` are pinned for the lifetime of a sprint run.

Cursor implementation uses `--force` and `--trust` only in verified harness worktrees under `.harness/worktrees/`; review and plan-repair calls do not use `--force`. Codex uses its own CLI flags and sandbox settings when enabled.

## Known gaps

- **`READY`** is a defined status but no live transition assigns it; scheduling uses `PENDING` and direct moves into `IMPLEMENTING` or waiting states.
- **Detached HEAD** as integration base is rejected (`current_branch` requires a named branch).
- **`execution.default_max_attempts`** defaults to 2 in config, but production always uses `ProviderAgentRunner`, which gates live implement calls via `has_implementation_budget` (often one Cursor call with default `codex.enabled: false` and empty `retry.sequence`). `harness retry` / `unblock` reset `attempt` but not `provider_attempts`; use `rerun-blocker` or `run-task --fresh-budget` for a real fresh implement budget.
