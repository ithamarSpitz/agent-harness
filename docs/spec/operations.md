# Operations

Post-sprint release steps and enqueueing work while a sprint is already running.

## Release

After a sprint finishes successfully, the harness may run optional shell commands on the **integration checkout** (the project root you run the harness from, not a task worktree).

### When release runs

`finalize_completed_sprint` runs at the end of a successful `run_sprint` loop (and from `harness run` / `harness resume` when every task is already `DONE` and the queue has been drained). Release runs only when all of the following hold:

| Condition | Meaning |
| --- | --- |
| Every task in the sprint is `DONE` | Checked by `sprint_is_complete`. |
| No pending queue entries | Nothing left under `.harness/queue/*.json` for that sprint (accepted/rejected entries do not block). |
| `release.commands` is non-empty | Empty list skips release entirely. |
| Not a dry run | `harness run --dry-run` does not execute release. |
| Automatic run not already recorded | State meta `{sprint_id}.release.status` must be unset; a failed automatic attempt leaves `failed` and does **not** retry until you run `harness release`. |

On finalize, the harness also sets `{sprint_id}.run_status` to `COMPLETED`, clears sprint failure records, and (when not dry-run) may record added tasks to the repo (see below) before release.

### Config

| Key | Default | Meaning |
| --- | --- | --- |
| `release.commands` | `[]` | Shell commands run in order on the integration checkout. |
| `release.timeout_minutes` | `15` | Per-command timeout passed to `Verifier.run_shell_commands` (each command may use the full limit). |

### Execution and state

`ReleaseRunner` runs commands via `Verifier.run_shell_commands` with log file `.harness/logs/{sprint_id}-release.log`. On success it sets:

| Meta key | Value |
| --- | --- |
| `{sprint_id}.release.status` | `done` |
| `{sprint_id}.release.completed_at` | UTC timestamp |

On failure it sets `status` to `failed`, plus `failed_command` and `failed_exit_code`, stops remaining commands, and **does not roll back** merged sprint commits.

`harness status` prints release outcome via `format_release_status` when meta is set (for example `release: done …` or `release: FAILED (…); retry with: harness release {sprint_id}`).

### Manual retry

```text
harness release <sprint>
```

Loads added tasks from state into the in-memory sprint (`merge_added_tasks_from_state`), requires every task `DONE`, refuses incomplete sprints (exit code 2), runs `release.commands` again via `run_and_persist` (exit code 1 on command failure, 0 on success). If `release.commands` is empty, prints that nothing is configured and exits 0.

## Adding tasks during a run

You can enqueue new briefs while a sprint is in progress. Accepted tasks behave like roadmap tasks for scheduling, verification, and merge. Acceptance sets `runtime.added = true` in state; `record_added_tasks_to_repo` sets `{sprint_id}.added_tasks_recorded` and does **not** clear `runtime.added`. `harness status` still prints `(added)` after the repo commit.

### Enqueue

```text
harness add-task <sprint> <brief> [--stage auto|current|next]
```

Default `--stage` is `auto`. Invalid values exit 2.

**Add-time validation** (`validate_brief_for_queue`, `require_incomplete_sprint=True`):

| Check | Error (representative) |
| --- | --- |
| Brief file missing or unreadable | `Task brief not found` / `Cannot read task brief` |
| Invalid or empty frontmatter | `Task brief frontmatter is missing or invalid` |
| Missing `id` | `Task brief frontmatter must include id` |
| Unknown sprint | Roadmap error text |
| Sprint already complete (all tasks `DONE`) | `Sprint {id} is already complete` |
| Duplicate task id in sprint | `Task id … already exists in sprint …` |
| `depends_on` not a list | `depends_on must be a list` |
| Unknown dependency ids | `Unknown dependencies for …` |
| Missing or empty `allowed_paths` | `allowed_paths must not be empty` |

On success, the brief bytes are written to `.harness/queue/{task_id}.md` and a sidecar `.harness/queue/{task_id}.json` with `sprint`, `stage_choice`, and `queued_at`.

### Pickup

`TaskQueueManager.process_pending` runs:

- While a stage’s thread pool is active (`watchdog_poll_seconds` polling), for the current stage with `merge_in_progress` matching whether that stage’s merge has started.
- At stage boundaries: before a stage starts (`merge_in_progress=false`) and after merge begins or completes (`merge_in_progress=true`), including a final drain when all tasks are `DONE` but the queue still has entries.

Pending entries are ordered so that dependencies between **queued** briefs are respected before placement.

**Stage choice** (`_choose_stage`):

| Choice | Behavior |
| --- | --- |
| `current` | Join `current_stage` only if merge has not started, all dependencies are `DONE`, and `allowed_paths` do not conflict with tasks already in that stage (including in-flight stage tasks). Otherwise reject at pickup with the current-path reason. |
| `next` | Place in the next stage number (after the current stage’s max, or `max+1` if already on the last stage), with dependency stage ordering rules and `allowed_paths` checks on the destination stage. |
| `auto` | Try `current`. If that cannot join (merge already started, dependencies not `DONE`, or current-stage `allowed_paths` conflict), **discard the current-path reason** and apply the same rules as `next`. `auto` during merge is placed in a following stage; it is not rejected for merge-in-progress. |

**Rejection at pickup** (brief and sidecar move to `.harness/queue/rejected/`; reason in `{task_id}.reason.txt`):

| Reason | Typical cause |
| --- | --- |
| `task id … already accepted` | Duplicate pickup |
| Validation errors | Same messages as add-time, but `require_incomplete_sprint=False` so briefs queued before sprint completion can still be picked up during barrier recovery |
| Brief parse / spec errors | From `task_spec_from_brief` |
| `stage merge already started` | `--stage current` while merge is in progress. `--stage auto` discards this reason and tries `next`. |
| `dependencies not DONE: …` | `--stage current` when some `depends_on` ids are not `DONE`. `--stage auto` discards this reason and tries `next`. |
| `allowed_paths conflict with …` | Overlap with another task in the target stage |
| Dependency stage ordering messages | Same-sprint dependency in same or later stage than the chosen stage |
| `rejected at pickup` | Fallback when no stage chosen |

**Acceptance** moves files to `.harness/queue/accepted/`, adds the task to the in-memory sprint, sets `runtime.added = true`, stores `tasks.{id}.stage` and `tasks.{id}.sprint` meta, increments `plan.revision`, and initializes task state.

Added tasks mid-stage join the active pool only when accepted into **the same** stage number as the running stage.

### Recording added tasks in the repository

When finalize runs and the sprint is complete, `record_added_tasks_to_repo`:

1. Skips if `{sprint_id}.added_tasks_recorded` is already set, or there are no added tasks (`runtime.added`).
2. If roadmap rows already match added tasks (each added id is present with the same `stage`):
   - Clean integration checkout (`git status --porcelain` empty): set `{sprint_id}.added_tasks_recorded` with no new commit.
   - Dirty checkout: return without setting the flag, without a commit, and without raising.
3. If roadmap rows do **not** already match, require a clean integration checkout; a dirty checkout raises `RuntimeError` (`Integration checkout is dirty; cannot record added tasks to the repository`).
4. Copies accepted briefs into the same directory as existing roadmap briefs (or `tasks/{sprint_id}/` if the sprint had no prior brief paths), updates `tasks/roadmap.yaml` with `file`, `stage`, and `depends_on`, and commits with message `harness: record added tasks for {sprint_id}`. Then sets `{sprint_id}.added_tasks_recorded`. `runtime.added` stays `true`.

Failed sprints do not record added tasks. Stage barrier rollback does not remove accepted added tasks from the sprint; they remain scheduled on resume.

### Commands that reload or recover added tasks

Commands that include added tasks:

| Command | Role for added tasks |
| --- | --- |
| `harness status` | Merges via `merge_added_tasks_from_state`. Shows `(added)` whenever `runtime.added` is true, including after recording; lists `REJECTED` queue outcomes with reasons. |
| `harness run` / `harness resume` | Merge before run; if all tasks are `DONE`, drain the queue (`merge_in_progress=true`) then finalize (record + release). |
| `harness rerun-blocker <sprint>` | On `STAGE_BARRIER`, merges added tasks and re-runs integration for `PASSED` tasks in the failed stage (including added tasks). On task `BLOCKED`, `--task` can target a blocked added task (`resolve_task` via `cmd_run_task`). After barrier recovery, `_pickup_queue` with `merge_in_progress=true` can accept tasks queued during the barrier before finalize. |
| `harness run-task`, `harness reset-task`, `harness inspect` | Resolve task specs via `task_queue.resolve_task` (roadmap or added). `resolve_task` loads added work from state / `.harness/queue/accepted/` when the on-disk roadmap does not list the id. |
| `harness retry`, `harness unblock` | Update runtime via `state.get(task_id)` only. They do not call `resolve_task` or load a `TaskSpec`. They still affect an accepted added task because it is already in `state.json`. |
| `harness release` | Merge before checking sprint completeness. |

Task ids for added work are unknown to the on-disk roadmap until recording succeeds; until then, state and `.harness/queue/accepted/` hold the source of truth.

## Known gaps

- **`harness retry` / `harness unblock` CLI text** says “fresh retry budget”, but only `attempt` is cleared; `provider_attempts` and `stall_counts` stay (see lifecycle spec). Added tasks follow the same rules as roadmap tasks.
- **Pending queue vs finalize**: If entries remain in `.harness/queue/` (not yet accepted or rejected), finalize—and therefore automatic release and repo recording—does not run even when every known task is `DONE`.
