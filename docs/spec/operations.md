# Operations

Machine-wide state, task control, post-sprint release, and enqueueing work while a sprint is already running.

## Machine-wide harness home and records

`HARNESS_HOME` selects the machine-wide harness directory. When unset it is `~/.harness`. This is separate from each project's runtime `.harness/`.

| File | Purpose |
| --- | --- |
| `agents.json` (with `agents.lock`) | Cross-process ledger of live agents and waiting projects, used for fair memory admission. Dead processes are pruned; corruption or lock/I/O failure warns and fails open. |
| `outcomes.jsonl` (with `outcomes.lock`) | Append-only, cross-project model outcomes used by `harness models report`. A shared-log failure warns once and never fails a task. |

Each provider call is still written to project-local `.harness/usage.jsonl`, and a normalized copy is appended to the shared outcomes log. Per-call fields are: `project`, `sprint`, `task_id`, `model_class`, `provider`, `model`, `effort`, `phase`, `attempt`, `ok`, `verification_ok`, `review_verdict`, `finding_count`, `input_tokens`, `cached_input_tokens`, `output_tokens`, `reasoning_output_tokens`, `duration_seconds`, and `recorded_at`. Unavailable values are JSON null.

Verification is a first-class usage call with provider `harness`, model `verification`, phase `verification`, attempt, `ok`, `verification_ok`, and duration. Final task records in the shared log have `event_type: task_final`, final status, implementation-attempt count, the provider/model/effort attempt list, passing attempt when known, total tokens, and total call duration.

`harness models report [--project PATH] [--since YYYY-MM-DD]` aggregates shared calls and task outcomes by provider/model/effort/phase. `harness models import PROJECT [PROJECT ...]` imports legacy project-local usage logs, marks imported rows, and deduplicates repeated imports. Tests redirect or disable both ledger and outcomes folders; they never read or write the real home folder.

## Holding and releasing one task

`harness hold TASK` records the task in the project runtime `.harness/hold-request`. The scheduler checks holds before every new agent call, after implementation/review calls, and immediately before commit. A held task is parked as `BLOCKED` with `held by operator`; it is not committed, and a hold observed with a quota/capacity result wins over the wait. Pending tasks do not start while held. Passed tasks cannot be held.

`harness release TASK [--fresh-budget]` removes the hold. With no live scheduler it changes a held task to `RETRY` immediately; a live scheduler applies the release without rewriting its state file. `--fresh-budget` resets attempt, per-provider attempts, and stall counts only when the hold actually took effect. Releasing before an in-flight call observes the hold cancels it and ignores the fresh-budget request. The existing `harness release SPRINT` behavior below remains available; the command resolves whether its argument is a sprint or held task.

## Brief reload and agent-reported blocks

Immediately before every implementation attempt, the scheduler rereads the authoritative project brief. It refreshes `allowed_paths`, `verification`, `timeout_minutes`, `review`, `allow_protected`, and `context`. Changes to id, stage, or dependencies are logged and deferred until a new start. Invalid/unreadable frontmatter warns and retains the last valid settings.

After an implementation call, a `STATUS` label (case-insensitive, optional colon) whose value is exactly `blocked` on that line or the next makes the task `BLOCKED`, even if the call succeeded or later verification would fail. The harness stores the reported `OPEN_ISSUES` body as the task error and does not spend further retries; if that section is missing or empty it stores `Agent reported blocked without OPEN_ISSUES details`.

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
