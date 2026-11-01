# Agent Harness Starter v0.11.8

## BookNook changes: per-task provider fallback

- A Codex quota/auth/model-unavailable result now switches only the current task to Cursor (state key `<sprint>.<task>.provider.codex.disabled`). Every new task probes Codex first, so work returns to Codex as soon as its limit resets. The task that fell back keeps Cursor for its remaining attempts and its review.
- Quota phrases match typographic apostrophes (`You’ve hit your usage limit`), which Codex prints. Previously that message was misread as a capacity wait.

## v0.11.6 deterministic verification environment

- Explicit task/stage verification environments are now authoritative and no longer re-merge ambient `os.environ`.
- `verification.env` remains fallback-only; real task/runtime values win.
- Regression coverage reproduces ambient `DATABASE_URL` leakage on Windows.

## v0.11.5 provider-capacity and verification-env fix

- Cursor `resource_exhausted` / provider-overload responses are classified as temporary provider capacity, not included-usage quota exhaustion. Capacity waits preserve task/worktree state, do not consume implementation retry budget, and default to a short five-minute retry cadence.
- Capacity exhaustion during review resumes from verification/review and does not rerun a successful implementation.
- `harness.py quota status` now also reports provider-capacity waiting tasks, and `harness.py usage` separates quota pauses from capacity pauses.
- Verification subprocesses receive a validation-only fallback `DATABASE_URL` when none is supplied by the real task/runtime environment. Real environment values always win, so Docker/runtime database configuration is not overwritten.

## v0.11.4 Windows Cursor prompt transport fix

- On Windows, when Cursor resolves to the official `agent.cmd`/`.bat` launcher, the Harness keeps the long prompt out of argv and streams it over stdin. This prevents the Windows batch/PowerShell launcher chain from corrupting prompts or swallowing trailing `--trust` / `--force` flags.
- `scripts/probe-cursor-headless.py` performs a minimal real Cursor smoke probe in a temporary Harness-owned worktree before retrying a blocked implementation task.
- Regression coverage verifies stdin transport preserves multi-line prompts containing quotes and shell metacharacters while retaining `--trust` and `--force` in argv.

## v0.11.3 diagnostics-state cleanliness fix

- `.harness/diagnostics/` is ignored so forensic log archives do not make the integration checkout dirty or fail `doctor --full`.


## v0.11.2 Windows headless execution fix

- Implementation calls now add Cursor `--force` only inside verified harness-owned worktrees so non-interactive agents can edit files and run commands without approval prompts.
- Review and plan-repair calls remain read-only/unforced.
- `reset-task` now also clears that task from the active sprint failure record.


A small, task-oriented orchestrator for running Cursor coding agents against a Git repository. It deliberately keeps project knowledge in Markdown/YAML and Git instead of building a separate memory/RAG system.


## Windows portability fixes in v0.11.1

- Cursor/other external `.cmd` and `.bat` shims are launched through `cmd.exe` on Windows.
- Explicit Python-script commands are launched through the active Python interpreter.
- Docker Compose command execution uses the same cross-platform launcher.
- Harness regression fixtures no longer rely on Unix-only shebang/chmod, `test -f`, `printf`, or `false`.
- Windows paths embedded in YAML test fixtures are escaped safely.

## What is implemented

- Sprint/stage/task roadmap in YAML.
- Dependency validation and cycle detection.
- Parallel execution within a stage.
- Fresh Cursor CLI invocation per implementation attempt.
- Headless Cursor `--trust` is granted only to Git worktrees created and verified by the harness; never to the integration checkout or arbitrary directories.
- Windows keep-awake is held for the complete live `run`/`resume` lifecycle and released on success, failure, or interruption.
- One deterministic Git worktree per task, with configurable one-time setup commands.
- Durable `.harness/state.json` state for resume.
- Retry budgets and task timeouts.
- Allowed-path and protected-path enforcement.
- `review_required` paths force an independent review even when a task declares `review: false`.
- Task-specific and global mechanical verification.
- Fresh read-only reviewer invocation after mechanical checks.
- Atomic stage integration barrier: all tasks pass before their branches are merged.
- Stage-level verification after merge; any merge or stage-verification failure rolls the integration checkout back to the exact pre-stage commit.
- Automatic cleanup of successful worktrees.
- Logs for agent runs, reviews and verification.
- CLI for status, graph, run, resume, focused blocker recovery, retry, verify, review, cleanup and doctor.
- Per-task Docker/Compose isolation with deterministic `COMPOSE_PROJECT_NAME` namespaces.
- Durable failure reports with exact resume/focused-rerun commands.

## Project task status

The final NYC architecture and six-sprint blueprint are wired in. **S0-foundation through S5-acceptance-submission are now fully decomposed into executable task briefs** under `tasks/` and are present in `tasks/roadmap.yaml`. The complete six-sprint execution plan is runnable.

The harmless `demo` sprint remains in the roadmap solely for harness self-testing.

The starter also intentionally does not include a vector DB, embeddings, long-term agent memory, a dashboard, a queueing service or automatic architecture mutation.

## Requirements

- Python 3.11+
- Git
- Node.js 22+ for the NYC project tasks
- Docker Desktop / Docker Engine with Compose v2 for Docker-backed tasks
- Codex CLI authenticated to the intended account when `codex.enabled: true`
- Cursor CLI (`agent`) only when `cursor.enabled: true`; this repository currently keeps Cursor disabled
- A Git repository with the harness files committed before an automated run

Install Python dependencies:

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements-harness.txt
```

Check the environment:

```bash
python harness.py doctor --full
```

Provider enablement is explicit in `harness.yaml`. This repository currently sets `cursor.enabled: false`, so implementations, reviews, and plan repair stay on Codex and `doctor` does not require or probe Cursor. Re-enabling Cursor requires an intentional config change to `cursor.enabled: true`; the existing Cursor model/fallback configuration is retained in Git for that purpose.

For Cursor specifically, verify once outside the project that `agent status`, `agent models`, and a tiny headless `agent -p ... --trust` smoke call succeed. Real harness runs do not trust the integration checkout; `--trust` is injected only for registered harness-owned task worktrees.

Cursor documents `agent -p` as its non-interactive automation mode. Model names vary over time/account; run `agent models` and put the desired exact names in `harness.yaml` rather than baking model IDs into tasks.

## Try the graph without spending Cursor usage

```bash
python harness.py graph demo
python harness.py run demo --dry-run
```

Expected shape:

```text
Stage 1: D-T1
Stage 2: D-T2, D-T3
Stage 3: D-T4
```

The demo task files are executable examples. Running the demo **without** `--dry-run` will invoke Cursor agents and consume usage.

Live `run` and `resume` commands hold a temporary Windows keep-awake requirement for their entire lifetime when `execution.keep_awake_during_run: true`. Dry-runs do not acquire it. Quota waiting nests safely inside the run-level keep-awake context.

## Real-project setup after ARCHITECTURE.md is finalized

1. Keep the original assignment as `docs/assignment.md`.
2. Split the final architecture into small domain context files, while retaining `ARCHITECTURE.md` as the source/index if desired.
3. Copy `tasks/roadmap.example.yaml` to `tasks/roadmap.yaml`.
4. Write real task briefs from `tasks/TASK_TEMPLATE.md`.
5. Add stable project-wide commands to `harness.yaml`, for example `npm run typecheck`.
6. Add stage barriers such as `npm test`, Prisma validation, Docker build or smoke tests only where they are useful.
7. Commit all harness/spec/task files before the first automated run.

## Core commands

```bash
python harness.py status [SPRINT]
python harness.py graph SPRINT
python harness.py run SPRINT
python harness.py resume SPRINT
python harness.py run-task TASK_ID [--model-class escalation] [--fresh-budget]
python harness.py failure SPRINT
python harness.py rerun-blocker SPRINT [--task TASK_ID] [--model-class escalation]
python harness.py retry TASK_ID
python harness.py verify TASK_ID
python harness.py review TASK_ID
python harness.py doctor --full
```

`tasks/roadmap.yaml` currently contains the harmless demo DAG plus executable `S0-foundation` through `S5-acceptance-submission`.

## Execution semantics

### Task execution

For each task the harness:

1. creates a Git worktree from the stage's integration base commit;
2. verifies that the workspace is a registered direct child of `.harness/worktrees`, then starts a fresh `agent -p ... --trust` invocation there;
3. runs scope/protected-path checks and mechanical verification;
4. optionally starts a fresh reviewer agent in read-only Ask mode;
5. retries with a fresh agent context if verification/review fails;
6. commits the accepted task branch itself;
7. marks the task `VERIFIED`, not `DONE`.

### Stage integration barrier

Only after every task in the stage is `VERIFIED` does the harness merge those branches into the integration checkout. Then it runs stage verification. Tasks become `DONE` only after that barrier passes.

The barrier is atomic with respect to tracked Git state. Before the first merge the harness records the integration `HEAD`. If any merge fails, stage verification fails, or verification leaves non-ignored uncommitted files behind, the harness aborts any in-progress merge and resets/cleans the integration checkout back to that exact checkpoint. The task worktrees/branches remain available for diagnosis.

If a task exhausts its retry budget, that task becomes `BLOCKED` and the sprint stops at the current stage. Sibling tasks that already passed remain `VERIFIED`; they are not re-run after the blocker is fixed.

If merge/stage verification fails, the integration checkout is rolled back but the task implementations remain `VERIFIED`. The failure is recorded as a `STAGE_BARRIER` failure, so the barrier itself can be re-run without invoking coding agents again.

### Context

The prompt tells each agent to read `AGENTS.md`, then the current task file, then only the architecture context referenced by the task. The harness does not inject the whole architecture document into every prompt.

## Worktree setup

The orchestrator creates deterministic worktrees under `.harness/worktrees/` itself so it knows where to inspect diffs and how to integrate them. Cursor also has native worktree support, but using harness-owned Git worktrees makes the integration lifecycle explicit.

One-time setup commands are configurable in `harness.yaml` and run immediately after a new task worktree is created, before the agent starts:

```yaml
worktree:
  setup_commands:
    - npm ci
  setup_timeout_minutes: 10
```

The starter leaves the list empty until the real application's package setup is finalized. Setup output is written to `.harness/logs/<TASK>-worktree-setup.log`; a failed or timed-out setup blocks the task before any model call is made.

## Safety defaults

- The integration checkout **must be clean** before a real `run`/`resume`. There is intentionally no dirty-checkout override because atomic rollback uses a known clean checkpoint.
- `execution.base_ref`, when configured, is a start-point guard: the first run of a sprint refuses to start unless the current `HEAD` resolves to that ref. The recorded integration branch/base are then reused for resume semantics.
- Changing `execution.base_ref` in the middle of an in-progress sprint is rejected.
- `docs/assignment.md` is protected by default.
- Paths under `paths.review_required` force reviewer execution even when the task itself has `review: false`.
- Agents are told not to commit; the harness owns accepted commits.
- Merge conflicts are not automatically "solved" by an agent in v0.1. They roll the whole stage integration back and block the stage for human inspection.

## Tests for the harness itself

```bash
pytest -q tests/harness
```

The tests create temporary Git repositories and exercise the real worktree/verifier/stage-merge code, including parallel workers, worktree setup, forced review paths, `base_ref` guards, rollback after failed stage verification, rollback after a partial merge sequence, and an end-to-end CLI run through a fake `agent -p` executable. This validates the complete orchestration path without consuming Cursor usage.

Both forms are supported:

```bash
pytest -q tests/harness
python -m pytest -q tests/harness
```

## v0.2 pre-task hardening

The starter now includes the remaining architecture-independent guardrails that are useful before the real NYC task graph is generated.

### Static roadmap/task validation

Run:

```bash
python harness.py validate
```

The linter fails before any model call for problems such as:

- missing/duplicate task identity;
- unknown dependencies or dependency cycles;
- same-sprint dependencies that are not in an earlier stage;
- empty or repository-escaping `allowed_paths`;
- missing task verification commands;
- undeclared `model_class` values;
- task frontmatter `id` mismatches;
- missing context aliases/files.

Machine-readable reference schemas live in `schemas/roadmap.schema.json` and `schemas/task-frontmatter.schema.json`. The harness uses its own semantic validator because several important checks (dependency stages, files on disk, model classes and context aliases) cannot be expressed by JSON Schema alone.

### Full preflight

Basic:

```bash
python harness.py doctor
```

Before unattended execution:

```bash
python harness.py doctor --full
```

Full mode additionally checks a clean integration checkout, free disk space, configured project commands/environment variables, and `agent models`. A successful `agent models` call doubles as an authentication/availability check, and each configured non-null model name must appear in the listing.

Project-specific requirements are configured without changing harness code:

```yaml
doctor:
  required_commands: [node, docker]
  minimum_versions: {node: "22.0.0"}
  required_env: [DATABASE_URL]
  min_free_disk_gb: 2
```

### Stall watchdog

The hard per-task timeout remains the absolute upper bound. A second configurable watchdog can terminate and retry a genuinely idle invocation earlier:

```yaml
execution:
  stall_timeout_minutes: 12
  watchdog_poll_seconds: 10
  keep_awake_during_run: true
```

A task is considered inactive only when **both** Cursor CLI output and tracked/non-ignored repository file activity have been silent for the configured period. Set `stall_timeout_minutes: 0` to disable the watchdog while retaining the hard timeout.

### Run manifests

Every real sprint run creates:

```text
.harness/runs/<run-id>/manifest.json
```

It records the starting commit/branch, harness/Python/Cursor versions, model routing, and SHA-256 fingerprints for the roadmap, harness config, AGENTS, assignment, architecture context and task files. `resume` reuses the same run id and appends a `resumed_at` timestamp.

Inspect the active or most recent run:

```bash
python harness.py manifest SPRINT
```

### Recovery and cleanup

```bash
python harness.py inspect TASK_ID
python harness.py unblock TASK_ID
python harness.py reset-task TASK_ID
python harness.py cleanup --stale-worktrees
python harness.py cleanup --completed
```

`inspect` prints task/runtime/context/diff/log metadata. `unblock` gives a BLOCKED task a fresh retry budget. `reset-task` deletes an unintegrated task worktree/branch and returns it to PENDING; it deliberately refuses to reset a DONE task because that commit is already in integration history. Cleanup only removes harness-owned stale directories or explicitly retained worktrees for DONE tasks.

### Logical context aliases

Once the architecture is final, `harness.yaml` can map stable logical names to files:

```yaml
context:
  aliases:
    assignment: docs/assignment.md
    architecture.ingestion: docs/architecture/ingestion.md
    architecture.storage: docs/architecture/storage.md
```

Then a task can declare:

```yaml
context:
  - architecture.ingestion
  - architecture.storage
```

The validator guarantees that every alias resolves, and the resolved paths are injected into implementer/reviewer prompts. This keeps task briefs stable if a context file is later relocated.

### Usage accounting

Every agent invocation appends a factual local record to `.harness/usage.jsonl`: task, phase, attempt, model class/name, duration, result, timeout/stall state and output size.

```bash
python harness.py usage
```

The harness intentionally does **not** fabricate token/cost numbers when Cursor's selected output format does not expose them.

### Harness CI

`.github/workflows/harness-ci.yml` runs on harness/task changes and performs:

1. Python compile/static syntax checks;
2. roadmap/task/context validation;
3. the full harness test suite.

The same checks can be run locally with:

```bash
scripts/check-harness.sh
```

For the same lint/type-check steps used by CI, install the optional development requirements:

```bash
pip install -r requirements-harness-dev.txt
ruff check harness harness.py tests/harness
mypy harness
```

`scripts/check-harness.sh` runs these checks when the tools are installed and otherwise still runs compile validation, task/roadmap validation and the full test suite. GitHub Actions always installs the dev requirements, so CI always executes lint + type-check.


## v0.3 Docker environments and focused failure recovery

### Failure reports and exact recovery

A real run that stops writes a durable report under:

```text
.harness/failures/<SPRINT>.json
```

Show the concise reason and the recommended next command:

```bash
python harness.py failure sprint-2
```

For a single blocked task, rerun only that task with a fresh attempt budget and a more expensive model class:

```bash
python harness.py rerun-blocker sprint-2 --model-class escalation
```

If several parallel tasks are blocked, choose one explicitly:

```bash
python harness.py rerun-blocker sprint-2 --task S2-T4 --model-class escalation
```

Equivalent lower-level focused execution is available with:

```bash
python harness.py run-task S2-T4 --fresh-budget --model-class escalation
```

A focused retry reuses that task's existing worktree and prior code, but starts a fresh Cursor invocation. Successfully verified sibling tasks remain `VERIFIED`. After the blocker is fixed:

```bash
python harness.py resume sprint-2
```

`resume` skips every task already `DONE` or `VERIFIED`, re-enters the current stage, runs only unresolved work, crosses the stage barrier, and then proceeds to later stages. It never restarts the sprint from stage 1 merely because a later task failed.

For a failed integration barrier, `rerun-blocker` runs only merge + stage verification. It does not invoke implementer agents again:

```bash
python harness.py rerun-blocker sprint-2
```

### Docker / Compose environment isolation

Docker management is opt-in per task. A task that needs a managed integration environment declares:

```yaml
environment: docker
```

All harness-side environment settings live in `harness.yaml`:

```yaml
environment:
  docker_enabled: true
  compose_command: docker compose
  compose_files:
    - docker-compose.yml
  project_name_prefix: harness
  services: []
  validate_compose: true
  up_before_agent: false
  teardown_on_success: true
  teardown_on_blocked: true
  remove_volumes: true
  command_timeout_minutes: 10
  env: {}
```

For each task the harness injects a deterministic isolated namespace:

```text
COMPOSE_PROJECT_NAME=harness-s2-t4
HARNESS_TASK_ID=S2-T4
HARNESS_SPRINT_ID=sprint-2
HARNESS_STAGE=2
HARNESS_WORKTREE=...
```

Parallel tasks therefore do not share Compose networks, named volumes or containers. Stage-verification commands receive a separate namespace such as `harness-sprint-2-stage-2`. Avoid fixed host ports in task-level Compose environments; prefer service-to-service access on the Compose network.

When `environment: docker` is requested and `validate_compose` is enabled, the harness runs `docker compose ... config --quiet` before the agent. When `up_before_agent` is enabled it then runs an isolated `up -d --remove-orphans`. The same environment variables are passed to the Cursor process, worktree setup commands, task verification and reviewer. On completion/block the configured Compose project is torn down, optionally including volumes.

The Docker lifecycle is deliberately separate from application configuration:

| File | Responsibility |
|---|---|
| `harness.yaml` | agent/worktree/Docker orchestration settings |
| `.env.example` | application runtime configuration contract |
| `.env` | local secrets/overrides; never committed |
| `src/config/env.ts` (later) | typed parsing/defaults for application env |
| `docker-compose.yml` (later) | API/worker/Postgres topology |
| `Dockerfile` (later) | reproducible Node runtime |
| task/stage verification | proves the environment actually works |

Once Docker management is enabled, `python harness.py doctor --full` also validates that the configured Compose executable works and that every configured Compose file exists.

### Retry/stop summary

A task gets `default_max_attempts` total attempts unless it overrides the value. Agent exit failure, hard timeout, watchdog stall, verification failure, reviewer rejection, scope/protected-path violation or commit failure consumes an attempt and retries in a fresh model context while retaining the task worktree. Once the budget is exhausted the task becomes `BLOCKED`.

Environment/setup failures do not waste model calls: the task is blocked before the implementer starts. A blocked task prevents its stage from integrating and prevents dependent later stages from starting, but already verified sibling work is preserved.

Stage merge or stage-verification failure is different: task work remains `VERIFIED`; only the integration checkout is rolled back. That exact barrier can then be retried independently.

## Final NYC architecture wiring and executable S0/S1 graphs

The repository now contains the final `ARCHITECTURE.md` plus generated exact context modules under `docs/architecture/`. Regenerate/check them with:

```bash
python scripts/generate-architecture-contexts.py
python scripts/generate-architecture-contexts.py --check
```

The non-runnable planning blueprint is in `plans/sprints.yaml`. It defines all six sprints and their stage/parallel-workstream boundaries. All six sprints additionally have executable task briefs in `tasks/roadmap.yaml`. Validate the blueprint with:

```bash
python scripts/check-project-blueprint.py
```

Project planning artifacts:

- `plans/sprints.yaml`
- `plans/task-generation-policy.yaml`
- `plans/verification-matrix.yaml`
- `plans/environment-contract.yaml`
- `plans/path-policy.yaml`
- `plans/model-routing.yaml`
- `plans/risk-register.yaml`

Human-readable planning docs are under `docs/project/`.

`tasks/roadmap.yaml` now contains runnable `S0-foundation` through `S5-acceptance-submission` graphs plus the harmless `demo` graph. S0 has five foundation tasks. S1 has ten property-resolution tasks. S2 has nine ingestion-lifecycle tasks. S3 has five publication tasks across five stages. S4 has eight API/operations tasks across four stages. S5 has five acceptance/submission tasks across five serial stages.

`harness.yaml` is already wired to the final architecture context aliases and project guardrails. Docker environment orchestration remains disabled only until Sprint S0 creates the real `docker-compose.yml`; the environment manager itself is already implemented and tested.



## v0.7 S1 executable property-resolution graph

S1 is now fully decomposed into ten executable tasks. Inspect it with:

```bash
python harness.py validate S1-property-resolution
python harness.py graph S1-property-resolution
python harness.py run S1-property-resolution --dry-run
```

The S1 graph deliberately isolates the Prisma/property-identity hotspot in Stage 1, runs address normalization plus the five NYC source-client workstreams in parallel in Stage 2, integrates them through one resolver-orchestration task, then runs single-property HTTP and bounded bulk-BBL registration in parallel before the final Docker/PostgreSQL-backed behavior gate. Focused tests belong to the task that owns the behavior; the Stage-5 gate only wires the already-built components and reruns the complete S1 suite.

The roadmap may declare future `environment: docker` tasks while Docker task orchestration is still disabled before S0 completes. Static validation reports that condition as a warning; actual execution still refuses a Docker-backed task until the integrated S0 foundation enables `environment.docker_enabled`.

## v0.6.1 Windows unattended-run hardening

Two machine-readiness behaviors are now enforced by the harness:

- `cursor.trust_harness_worktrees: true` adds Cursor's `--trust` flag only when the workspace is a Git-registered worktree directly under `.harness/worktrees`. The repository root/integration checkout and arbitrary directories never receive implicit trust.
- `execution.keep_awake_during_run: true` holds the Windows execution-state requirement across the complete live `run`/`resume` call. The keep-awake implementation is nested/ref-counted, so entering `WAITING_FOR_QUOTA` cannot accidentally release the outer run-level requirement.

The project configuration starts with `execution.max_parallel_agents: 2` for the initial machine profile; this can be raised later after observing RAM pressure during Docker-backed parallel tasks.

## v0.6 S0 executable task graph

S0 is the first real runnable project sprint. Validate/inspect it with:

```bash
python harness.py validate S0-foundation
python harness.py graph S0-foundation
python harness.py run S0-foundation --dry-run
```

The five S0 tasks deliberately keep tests next to the behavior/foundation they own. The final S0 task performs the first real PostgreSQL/Docker integration gate and, once integrated, switches future worktrees to `npm ci` and enables per-task Docker isolation.

## v0.5 plan repair and quota-safe continuation

### Change control after execution has started

A BLOCKED task has two recovery paths. A focused implementation retry uses `rerun-blocker` / `run-task --model-class escalation`. If evidence points to a missing task or an architectural conflict, use the explicit human-gated plan-repair flow instead:

```bash
python harness.py plan-change propose <SPRINT> --task <TASK_ID>
python harness.py plan-change status
# review proposal; edit and COMMIT ARCHITECTURE/roadmap/task files
python harness.py plan-change apply <CR-ID> --affected <TASK_ID>
python harness.py resume <SPRINT>
```

The planner is read-only. It must classify the problem as `IMPLEMENTATION_ONLY`, `TASK_ADDITION`, `ARCHITECTURE_CHANGE`, or `UNKNOWN`; it never edits protected planning files itself. Applying a proposal increments the persisted plan revision, snapshots the revised roadmap/architecture hashes, discovers added/changed task definitions, resets only affected non-integrated tasks, and queues already-integrated affected tasks for revalidation. Started tasks cannot simply disappear from a revision; supersede/repair them instead.

`resume` always walks from the earliest unfinished stage. Existing `DONE` tasks are skipped. A newly added prerequisite/task in an earlier stage therefore runs without replaying unrelated completed work. Run manifests record plan revision changes.

### Cursor quota exhaustion

Cursor currently does not expose a reliable official CLI percentage for remaining included usage. The harness therefore does not pretend it can stop exactly at 10%. It recognizes configurable quota-exhaustion messages and persists a non-failure pause:

```text
RUNNING -> WAITING_FOR_QUOTA -> RUNNING
```

Quota exhaustion does **not** consume implementation retry budget. Worktrees/state are retained and managed Docker environments can be torn down while waiting. If quota is exhausted during independent review, resume re-runs verification/review without calling the implementation model again.

Configuration:

```yaml
quota:
  policy: wait              # wait | stop
  reset_at: null            # optional exact ISO time from Cursor dashboard
  probe_interval_minutes: 60
  reset_grace_minutes: 5
  keep_awake: true
  teardown_environment_while_waiting: true
```

Useful commands:

```bash
python harness.py quota status
python harness.py quota set-reset 2026-10-01T12:30:00+03:00
python harness.py quota clear-reset
```

With `policy: wait`, the same harness process sleeps until the configured reset (+ grace). If no reset time is known it retries on the configured conservative probe interval. On Windows, the harness temporarily requests that the system stay awake only while it is quota-waiting. `Ctrl+C` is safe; a later `resume` continues from persisted state. No on-demand billing/fallback is used.

### Temporary provider capacity

A provider-side `resource_exhausted`/overload response is deliberately **not** treated as account quota. It transitions the affected task to `WAITING_FOR_CAPACITY`, preserves the same worktree and retry budget, and retries on the shorter `capacity.retry_interval_minutes` cadence. If capacity is exhausted during review, resume repeats verification/review without another implementation call. Explicit quota phrases such as `quota exceeded` still take precedence and use `WAITING_FOR_QUOTA`.

```yaml
capacity:
  policy: wait
  retry_interval_minutes: 5
  keep_awake: true
  teardown_environment_while_waiting: true
```

`python harness.py quota status` reports both quota and capacity wait state.

### Verification-only environment fallbacks

`verification.env` supplies values only when the caller/task environment does not already define them. The starter uses a non-routable validation-only PostgreSQL URL so commands such as `prisma validate` can parse the datasource outside Docker without requiring operators to export `DATABASE_URL` in every shell. Docker/task runtime values always win and are never replaced by this fallback.

## v0.6.2 automatic model escalation

The project now pins the verified Cursor model IDs from the target account and uses a deterministic automatic implementation sequence:

```text
attempt 1  worker       -> composer-2.5
attempt 2  worker       -> composer-2.5
attempt 3  hard_worker  -> cursor-grok-4.6-high
attempt 4  escalation   -> claude-opus-5-thinking-high
attempt 5  -> does not exist; task becomes BLOCKED / plan-repair candidate
```

Every implementation attempt is a fresh Cursor invocation over the same task worktree. Failed-test/reviewer evidence from the prior attempt is included in the next prompt. Quota exhaustion does not advance the sequence because it does not consume an attempt.

Independent review defaults to `reviewer -> cursor-grok-4.6-high`. A future low-risk/simple task may explicitly declare `review_model_class: worker` to use Composer for review; protected/critical paths still force a review. Fast model variants are intentionally not used.

A manual focused override such as:

```bash
python harness.py run-task S2-T7 --fresh-budget --model-class escalation
```

is deliberately **one-shot**. It cannot silently spend four Opus calls. If that focused invocation fails, the task remains blocked for inspection/plan repair.


## v0.8 S2 executable ingestion-lifecycle graph

S2 is decomposed into nine executable tasks. Inspect it with:

```bash
python harness.py validate S2-ingestion-lifecycle
python harness.py graph S2-ingestion-lifecycle
python harness.py run S2-ingestion-lifecycle --dry-run
```

The graph isolates the Prisma/config/source-identity hotspot in Stage 1. Stage 2 runs the Socrata client, bounded Bottleneck/request-retry executor, raw->strict-normalization->staging pipeline, and dedicated advisory-lock authority primitive in parallel. Stage 3 runs crash-safe run initialization and the replay-safe persisted-batch processor in parallel because both consume Stage-2 primitives but do not own each other's files. Stage 4 composes them into the resume/fail-stop ingestion executor. Stage 5 provides the Docker/PostgreSQL-backed lifecycle gate and explicit manual source-contract probe. S2 deliberately stops at a typed S3 publication boundary: no live ECB promotion, negative reconciliation, or property coverage publication is implemented here.


## S3 — Ingestion Publication Tasks

`S3-ingestion-publication` is executable after S2. Its graph is:

```text
Stage 1: S3-T1 live-state promotion/reconciliation
Stage 2: S3-T2 successful coverage + identifier-version guards
Stage 3: S3-T3 atomic accepted-run publication
Stage 4: S3-T4 atomic terminal failure publication
Stage 5: S3-T5 full publication semantic gate
```

S3 deliberately owns database publication semantics only. S4 still owns HTTP query/pagination, scheduler/manual operational entrypoints, and production-shaped API runtime behavior.


## S4 — API & Operations Tasks

`S4-api-operations` is executable after S3. Its graph is:

```text
Stage 1: S4-T1 property ECB local query + NULL-aware pagination
         S4-T2 portfolio ECB membership/updatedSince query
         S4-T3 scheduler + manual ingestion CLI
         S4-T4 HTTP security/error middleware
Stage 2: S4-T5 property ECB HTTP endpoint
         S4-T6 portfolio ECB HTTP endpoint
Stage 3: S4-T7 final application/Docker runtime assembly
Stage 4: S4-T8 integrated API/operations behavior gate
```

S4 keeps all ECB GET paths local-store-only, proves the property cursor can cross from dated rows into the `NULL` tail without skips/duplicates, enforces current portfolio membership separately from `is_current`, and wires both scheduled and manual ingestion to the same S2/S3 executor. Real NYC acceptance, BIS spot checks, submission evidence and the 10,000-property run remain S5.

## v0.11 S5 executable acceptance/submission graph

S5 is the final executable sprint. Inspect it with:

```bash
python harness.py validate S5-acceptance-submission
python harness.py graph S5-acceptance-submission
python harness.py run S5-acceptance-submission --dry-run
```

The graph is deliberately serial because every later stage consumes measured evidence from the previous one:

```text
Stage 1: S5-T1 acceptance fixtures + reproducible small-run/evidence tooling
Stage 2: S5-T2 real small baseline + BIS spot checks + idempotent second-run evidence
Stage 3: S5-T3 deterministic 10,000-BBL PLUTO seed + real scale evidence
Stage 4: S5-T4 README/DESIGN/RUN_LOG built only from committed evidence
Stage 5: S5-T5 adversarial clean-repository Docker/reference-property final gate
```

Expensive external work is not repeated by ordinary Harness verification. S5-T2 and S5-T3 execute their live NYC/BIS/scale work once and commit raw logs plus machine-readable summaries; their mechanical verification paths validate those artifacts. If BIS is unavailable, a scale run fails, or a live fixture exposes a product defect, the task blocks with preserved diagnostics instead of fabricating a pass. The final gate does not rerun the 10k benchmark, but it does start from isolated clean Docker state and replays the Empire State resolve -> manual ingestion -> local ECB query evaluator flow.



## v0.12.5 Codex implementation profiles

Codex implementation routing is now Sol Medium, Sol Medium, Sol High, Astra Medium. Model and reasoning effort are pinned per implementation attempt; review and plan-repair continue to use the existing global Codex effort and Sol model. Cursor routing is unchanged. Explicit Codex quota/auth/model-unavailability still switches the current run to the existing Cursor Composer/Composer/Grok/Opus ladder without consuming a provider-switch attempt, while capacity/network/timeout/CLI-transient failures stay on the same Codex profile without consuming provider/model budget.

## v0.12.4 Codex-first provider routing

Codex CLI became the primary provider when ChatGPT authentication is available. The original v0.12.4 implementation routing was Luna High, Luna High, Terra High, Sol High. Explicit Codex quota/auth/model-unavailability switches the current run to the existing Cursor Composer/Composer/Grok/Opus ladder without consuming a provider-switch attempt. Capacity/network/timeout/CLI-transient failures stay on the same provider/model and use the existing capacity wait path. Reviews and plan-repair analysis also prefer Codex Sol with Cursor fallback. `harness.py usage` separates providers.

### Windows Codex sandbox note (v0.12.4)

On native Windows, Codex implementation calls can opt into `codex.windows_implementation_sandbox`. The NYC project sets this to `danger-full-access` because current native-Windows `workspace-write` runs can leave generated files unreadable to the parent Harness process via ACL/sandbox regressions. This override applies only to implementation calls inside Harness-owned task worktrees. Review and plan-repair calls remain `read-only`, and the Harness still enforces allowed/protected paths, verification, review, and atomic rollback. Non-Windows implementation calls continue to use `codex.sandbox` (`workspace-write`).

## Operating the harness

Read `HARNESS-OPERATOR.md` before starting, resuming or repairing a run.
