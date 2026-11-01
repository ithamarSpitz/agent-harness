---
id: S7-T1
stage: 1
model_class: hard_worker
timeout_minutes: 60
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

`harness add-task <sprint> <brief.md> [--stage auto|current|next]` adds a task to a sprint while that sprint is running (or paused), without stopping it and without editing the plan files in the repository during the run.

# Background

Today a sprint's tasks come from `tasks/roadmap.yaml` and the briefs under `tasks/`, both tracked in the repository. During a run the integration checkout must stay clean, and a failing stage rolls `main` back to the stage's starting commit, so editing or committing plan files mid-run either blocks the merge or gets erased. In `Scheduler.run_sprint` each stage submits all its unfinished tasks to a thread pool at once and waits for all results before `_integrate_stage`.

# Decisions (do not reopen)

1. **Queue outside Git.** `add-task` validates the brief, then writes it atomically (temp file, then rename) to `<runtime root>/queue/<task id>.md` with a sidecar `<task id>.json` holding `{"sprint": ..., "stage_choice": "auto"|"current"|"next", "queued_at": ...}`. It never touches the integration checkout or `state.json`, so it is safe while another harness process runs. It prints where the task was queued.
2. **Validation at add time** (refuse with a clear message, queue nothing): the frontmatter parses like any task brief; the `id` is new in the sprint (roadmap and earlier queued tasks); every `depends_on` exists in the sprint; `allowed_paths` is not empty; the sprint exists and is not complete.
3. **Pickup by the running harness.** The scheduler process is the only writer of state. It checks the queue (a) before starting each stage and (b) while a stage's tasks are running, every `execution.watchdog_poll_seconds`: change the stage's single `as_completed` wait into a loop that waits with that timeout and checks the queue between waits. A paused sprint picks up its queue on the next `run` / `resume`.
4. **Placement.**
   - `current`: join the stage that is running now, only if that stage has not started its merge (`_integrate_stage`), the task's `allowed_paths` do not conflict with any task of that stage (same rule as `_tasks_may_conflict`), and all its `depends_on` are already DONE. If any condition fails, reject it.
   - `next`: run in the stage after the current one; if the current stage is the last, a new stage is created after it.
   - `auto` (default): `current` when its conditions hold, otherwise `next`.
   A joined task is submitted to the same pool, so `max_parallel_agents` still limits how many agents run.
5. **Accept or reject.** On pickup the scheduler re-validates, records the task in `state.json` (spec path, chosen stage, `added: true`, plan revision bumped through the existing `plan.revision` meta), and moves the files to `<runtime root>/queue/accepted/`. A rejected entry moves to `<runtime root>/queue/rejected/` with a `<task id>.reason.txt`. `status <sprint>` lists added tasks with `(added)` and rejected ones with their reason.
6. **Agents read the queued brief.** The task's worktree is created from the stage base, which does not contain the new brief. The implement and review prompts point to the brief's absolute path in `queue/accepted/`. Nothing is copied into the worktree.
7. **Resume works.** After a crash or restart, tasks recorded as added in `state.json` are part of the sprint exactly like roadmap tasks.
8. **Record in the repo after the sprint.** When the sprint completes, the harness copies each added brief to `tasks/<sprint directory of the sprint's first task>/` (same filename as queued), appends the task to the sprint in `tasks/roadmap.yaml` with its final stage and `depends_on`, and commits that as `harness: record added tasks for <sprint>`, before the release step from S5. If the sprint does not complete, nothing is written to the repo.
9. **Unchanged:** stage atomicity (a joined task that fails makes its stage fail and roll back like any task), the stage barrier, review, verification and release.
10. **README.** A short "Adding a task to a running sprint" section, including the risk that a task joining a running stage can make that stage roll back.

# Out of scope

Removing or editing tasks during a run, changing an added task after it is queued, priorities, a UI.

# Acceptance criteria

Tests use the existing fake-agent harness setup:

1. A task added during a running stage with no overlap joins that stage, runs, and is merged with it.
2. An overlapping task with `auto` goes to the next stage; with `current` it is rejected with the conflict named; adding to a stage whose merge already started is never `current`.
3. `next` on the last stage creates a new final stage.
4. Invalid briefs (bad frontmatter, duplicate id, unknown dependency, empty `allowed_paths`, completed sprint) are refused at add time and nothing is queued.
5. `add-task` while the scheduler runs never writes `state.json` or the integration checkout (`git status` stays clean).
6. Kill-and-resume keeps an accepted added task.
7. After the sprint completes, the brief and roadmap entry are committed once; a failed sprint writes nothing to the repo.
8. A failing added task in a joined stage rolls back that whole stage, as today.
