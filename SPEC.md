# Agent harness specification

This document is the entry point for how the harness behaves today, as implemented under `harness/`. Detailed behavior lives in linked parts; this file does not duplicate them.

## Purpose

The harness orchestrates coding agents (Cursor CLI and optionally OpenAI Codex CLI and Claude Code) against any Git project. Work is expressed as a **roadmap of task briefs** with mechanical verification and optional independent review. Each task runs in an isolated **Git worktree**; successful task commits merge into the **integration checkout** (the branch you run from) only after every active task in a **stage** reaches `PASSED`. Stages are **atomic**: merge or stage-verification failure rolls the integration checkout back to the pre-stage checkpoint. Agents do not commit or merge; the harness does.

Core ideas: plan as data (`tasks/roadmap.yaml` and briefs), parallel tasks within a stage when `allowed_paths` do not conflict, headroom-aware routing across Codex, Cursor, and Claude ladders, and human-gated **plan repair** when blocked work indicates a plan gap.

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
| **Runtime directory** | Project-local `.harness/` (gitignored): state, logs, queue, worktrees, code-map build, failures, manifests, usage, plan revisions. Machine-wide ledger and outcomes live under `HARNESS_HOME` (default `~/.harness`). |

State persists in `.harness/state.json`. Logs live under `.harness/logs/`.

## Parts

| Part | Contents |
| --- | --- |
| [Task lifecycle and stage barrier](docs/spec/lifecycle.md) | Statuses, transitions, retries, integration barrier, recovery commands. |
| [Providers, quota, and capacity](docs/spec/providers.md) | Antigravity, Codex, Cursor and Claude Code; ladders, permissions, headroom/urgency routing, reserves, waits, and fallback. |
| [Review and code map](docs/spec/review.md) | Independent cross-provider review, required impact analysis, UI check, optional Graphify code map. |
| [Operations](docs/spec/operations.md) | `HARNESS_HOME`, usage/outcome records, provider limits/headroom, hold/release, brief reload, post-sprint release, and live task enqueueing. |
| [Configuration](docs/spec/configuration.md) | All `harness.yaml` keys, including agy, Claude and Cursor Auto, plus task brief frontmatter. |

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

## S8 execution behavior

### Prompts, retries, waits, and failures

- Implement, review, and plan-repair prompts embed the current task brief read from its absolute project path. The embedded copy wins if the task worktree contains an older copy; an unreadable brief falls back to displaying its path. User-facing paths and plan-change records remain project-relative where possible.
- A provider-capacity result is retried inside the current stage after `capacity.retry_interval_minutes` while another task in that stage is still running. A due retry gets a free worker before new pending work. When every unresolved task is capacity-paused, control returns to the outer capacity-wait loop. These pauses preserve the worktree and do not spend task retry budget.
- Quota waiting first honors `quota.reset_at` plus `quota.reset_grace_minutes`. Otherwise it reads live Codex and Cursor limits: for each provider it uses the latest reset among that provider's exhausted windows, then waits for the earliest provider to become usable, plus one minute, bounded to 24 hours. If limits cannot provide a future reset, it falls back to `quota.probe_interval_minutes`. A task's temporary Codex-to-Cursor quota fallback expires after the wait so Codex is probed again.
- Cursor quota matching includes straight and typographic forms of “you're out of usage.” Network, provider-busy, provider-error, and timeout waits remain distinct states. Cursor `stream-json` output reconstructs concatenated assistant messages with newlines so a review's line-oriented `VERDICT` is preserved.
- A CLI launch failure is not an implementation failure and does not spend a provider attempt. Codex may fall back to Cursor in the same call; if no alternate can start, the task becomes `BLOCKED` with a `harness setup-machine` hint. Windows process resource exit codes are requeued as resource-capacity failures without spending an attempt; five consecutive resource launch failures block the task.
- Each task, global-task, stage, and integrated-task verification command has the independent limit `execution.verification_timeout_minutes`. On expiry the harness kills the command's process tree and reports the command and timeout.

### Worktrees, code maps, and UI checks

- Existing task worktrees are reused only when Git registers the exact expected path and it is attached to the expected task branch. Missing, stale, linked, wrong-branch, or differently registered paths are rejected or safely recreated; resume revalidates the recorded worktree before invoking an agent.
- Stale cleanup prunes Git metadata and removes only unregistered children of `.harness/worktrees/`. Symlinks and junctions there are unlinked without traversing their targets. Worktree removal retries Windows read-only and sharing failures before reporting an error.
- The shared code-map cache `.harness/code-map/` and copied `graphify-out/` directory are added to the repository's Git common-dir `info/exclude`, so they cannot dirty the integration checkout or task worktrees.
- With `ui_review.enabled`, implementers as well as reviewers receive the UI-check instructions. Codex disables bundled/user browser plugins and injects only the configured Playwright MCP server for implement/review calls; Cursor auto-approves MCP use and warns once if its MCP configuration does not mention Playwright. Plan repair receives no browser tooling.
- UI cleanup runs after every implement and review call, including failure paths. It stops new configured-port listeners and processes launched from the task worktree while preserving listeners that existed before the call; cleanup failure is logged and does not replace the agent result.

### Memory admission and self-hosting

- A new agent starts only if the project is below `execution.max_parallel_agents` and estimated available memory is sufficient. Available memory is free memory minus `execution.min_free_memory_gb` and warm-up reservations for agents started in the last three minutes. The first agent for a project is always admitted so a project cannot starve. If free-memory detection fails, scheduling falls back to the per-project parallel limit.
- The initial estimate is `execution.memory_per_agent_gb`. The scheduler samples the agent process tree, learns the largest observed peak into state metadata, and subsequently uses the larger learned value. The board and `status` display the active estimate.
- `~/.harness/agents.json`, guarded by a cross-platform lock, records live agents and waiting projects across all harness processes. Dead-process records are removed. A project that already has more agents yields a start to a waiting project with fewer agents. Ledger corruption or lock/I/O failure logs a warning and fails open with an empty ledger; short Windows sharing violations are retried.
- With `self_hosting.enabled: true`, a successfully integrated non-final stage pauses with `RESTART_ON_NEW_CODE`. The CLI installs that commit into `.harness/self/<short-commit>/`, starts `python -P -m harness resume` with that frozen copy first on `PYTHONPATH`, and retains the three newest copies. Install failure is a warning and the existing process continues on its current code.
- This repository's configured release sequence runs `scripts/bump_version.py` before committing, pushing, and reinstalling. The script defaults to a minor semantic-version bump (`--part patch` is also supported), updates exactly one `harness/__init__.py` version assignment atomically, and fails without a partial edit if the version line is invalid.

## Board, events, stop, and console

`harness run SPRINT` and `harness resume SPRINT` use a live board when stdout is an ANSI-capable TTY. `--no-board` forces the traditional progress output. `harness watch [SPRINT]` attaches to persisted state (or discovers the active sprint), reloads it from disk, and exits when the run finishes; it does not run tasks. Watch also participates in keep-awake handling.

The board shows the sprint and current stage, running/max parallelism, free memory and the per-agent estimate, every stage/task status, provider/model for active agent phases, attempt count, elapsed phase time, dependencies, dynamically added tasks, merged stages, and the five newest events. Status colors are disabled by `NO_COLOR`; narrow or non-Unicode terminals degrade by truncating rows and using ASCII markers.

Events are appended to `.harness/events.jsonl` for task state changes, added tasks, stage merges, release state, stop requests, and self-host restarts. Readers filter by sprint and show newest first. Once the file exceeds 1,000 lines it is trimmed to the newest 500.

When stdin and stdout are both TTYs, the bottom board row is an interactive `harness>` console. It supports line editing and these commands:

| Command | Behavior |
| --- | --- |
| `help` | List console commands. |
| `status` | Clear command output; status is already on the board. |
| `task ID` | Show status, provider/model, attempt, elapsed time, paths, dependencies, and whether the task was added. |
| `why ID` | Show the latest review finding, task error, or sprint failure. |
| `log ID` | Show the latest agent message parsed from the task's newest agent log. |
| `diff ID` | Show `git diff --stat` against the task worktree's base. |
| `usage` | Show the same provider limit output as `harness limits`. |
| `add BRIEF [--stage auto|current|next]` | After confirmation, queue a task through `harness add-task`. |
| `retry ID` | After confirmation, rerun a `BLOCKED` task through `harness rerun-blocker`. |
| `stop` | After confirmation, request a drain-and-pause. Running agents finish, no new agents start, state is preserved, and `harness resume` continues later. |
| `quit` | In watch mode, leave the console only. During a run, confirm the same drain-and-pause behavior as `stop`. |

State-changing console commands require `y`; Enter, Escape, or `n` cancels. Ctrl+C restores terminal mode and safely pauses a run (or exits watch). The `.harness/stop-request` payload names its sprint and is consumed after in-flight agents drain.

## S8 configuration reference

All S8 keys have backward-compatible defaults:

| Key | Default | Meaning |
| --- | ---: | --- |
| `execution.min_free_memory_gb` | `2.0` | Free-memory reserve retained before additional agents start; must be positive. |
| `execution.memory_per_agent_gb` | `1.5` | Initial per-agent admission estimate; must be positive and may be superseded by the learned peak. |
| `execution.verification_timeout_minutes` | `30.0` | Per verification command timeout; must be positive. |
| `self_hosting.enabled` | `false` | Restart between non-final stages on a frozen copy of newly integrated harness code. |
| `ui_review.playwright_mcp_package` | `@playwright/mcp@0.0.84` | Playwright MCP package injected into Codex UI-capable calls. |

## Known gaps

- **`READY`** is a defined status but no live transition assigns it; scheduling uses `PENDING` and direct moves into `IMPLEMENTING` or waiting states.
- **Detached HEAD** as integration base is rejected (`current_branch` requires a named branch).
- **`execution.default_max_attempts`** defaults to 2 in config, but production always uses `ProviderAgentRunner`, which gates live implement calls via `has_implementation_budget` (often one Cursor call with default `codex.enabled: false` and empty `retry.sequence`). `harness retry` and `harness unblock` reset `attempt`, `provider_attempts`, and `stall_counts`, providing a fresh implementation budget; `rerun-blocker` and `run-task --fresh-budget` do the same.
