# Agent harness specification

This document describes what the harness is and how it behaves today, as implemented under `harness/`. It is not a roadmap or marketing summary.

## Purpose

The harness orchestrates coding agents (Cursor CLI and optionally OpenAI Codex CLI) against any Git project. Work is expressed as a **roadmap of task briefs** with mechanical verification and optional independent review. Each task runs in an isolated **Git worktree**; successful task commits merge into the **integration checkout** (the branch you run the harness from) only after every task in a **stage** passes. Stages are **atomic**: failed stage verification rolls the integration checkout back to the pre-stage commit. Agents do not commit or merge; the harness does.

Core ideas: plan as data (`tasks/roadmap.yaml` and briefs), parallel tasks within a stage when `allowed_paths` do not conflict, retry and provider fallback without silently widening scope, and human-gated **plan repair** when blocked work indicates a plan gap.

## Concepts

| Term | Meaning |
| --- | --- |
| **Project** | The Git repository root the harness is run from (`--root`, default `.`). |
| **Roadmap** | YAML file (default `tasks/roadmap.yaml`) listing sprints, task ids, brief paths, and per-sprint `stage_verification`. |
| **Sprint** | A named unit of work (`SprintSpec`) containing tasks and optional stage-level verification commands. |
| **Stage** | Integer barrier group. All non-`DONE` tasks in the lowest open stage run (up to `max_parallel_agents`), then merge together. |
| **Task** | One brief (`TaskSpec`): scope, verification, model class, dependencies, etc. |
| **Brief / frontmatter** | Markdown file with YAML frontmatter (`---` … `---`) parsed for scheduling and agent prompts. Roadmap rows merge with brief frontmatter (roadmap wins on overlapping keys when loading from roadmap). |
| **Worktree** | Per-task directory under `.harness/worktrees/<task-id>/` on branch `agent/<task-id>`, created from the stage base commit. |
| **Integration checkout** | The main repo at `--root` on a named branch (not detached HEAD). Stage merges land here. |
| **Runtime directory** | `.harness/` (gitignored): state, logs, queue, worktrees, code-map build, failures, manifests, usage, plan revisions. |

State persists in `.harness/state.json`. Logs live under `.harness/logs/`.

## Task lifecycle

### Status values

| Status | Meaning |
| --- | --- |
| `PENDING` | Not started (default). |
| `READY` | Defined in the enum and accepted when loading legacy persisted state; the scheduler does not assign this status during normal runs. |
| `IMPLEMENTING` | Agent implementation call in progress. |
| `CHECKING` | Task verification (diff scope, protected paths, commands) running. |
| `REVIEWING` | Independent reviewer agent running. |
| `PASSED` | Task checks and review (if required) passed; commit recorded in worktree; waiting for stage merge. |
| `MERGING` | Stage integration in progress for this task. |
| `RETRY` | Last attempt failed; will retry if attempt budget remains when the task is run again. |
| `DONE` | Task branch merged and stage barrier succeeded. |
| `BLOCKED` | Attempt budget exhausted or hard failure (e.g. three stalls without provider fallback); needs recovery. |
| `WAITING_QUOTA` | Provider account quota exhausted; worktree/state preserved; retry budget not consumed for that pause. |
| `WAITING_NETWORK` | Transient network failure; same preservation rules as capacity waits. |
| `WAITING_PROVIDER_BUSY` | Provider capacity / overload; same preservation rules. |
| `WAITING_PROVIDER_ERROR` | Other transient provider errors (and legacy alias `WAITING_FOR_CAPACITY` maps here). |
| `WAITING_TIMEOUT` | Hard timeout or stall watchdog fired; may trigger provider fallback before blocking. |

Legacy persisted names `RUNNING`, `VERIFYING`, `VERIFIED`, `WAITING_FOR_QUOTA`, `WAITING_FOR_CAPACITY` map to the statuses above when state is loaded.

### Transitions (normal stage run)

| From | To | Cause |
| --- | --- | --- |
| `PENDING` / `RETRY` | `IMPLEMENTING` | Task started; `attempt` incremented. |
| waiting statuses with `waiting_phase` not `"review"` | `IMPLEMENTING` | Implement-phase resume; `attempt` incremented. Implement-phase quota/capacity pauses decrement `attempt` when pausing, so that slot is reused. |
| waiting statuses with `waiting_phase == "review"` | `CHECKING` or `REVIEWING` | `run`/`resume` calls `_verify_review_commit`. No new implement call. If worktree HEAD and the recorded verification fingerprint still match, verification is skipped and review continues (`REVIEWING`); otherwise verification re-runs (`CHECKING`) then review. Applies to every `WAITING_*` in review phase (quota, capacity/transient, `WAITING_TIMEOUT` after a reviewer stall). |
| `IMPLEMENTING` | `CHECKING` | Implementation agent returned success. |
| `IMPLEMENTING` | `RETRY` | Implementation agent failed (non-wait). |
| `IMPLEMENTING` | `WAITING_*` | Quota or capacity/transient wait during implement (`waiting_phase=implement`). |
| `CHECKING` | `REVIEWING` | Verification passed and review required. |
| `CHECKING` | `PASSED` | Verification passed, review not required, commit succeeded. |
| `CHECKING` | `RETRY` | Verification failed. |
| `REVIEWING` | `PASSED` | Review output contains a line exactly `VERDICT: PASS` and commit succeeded. |
| `REVIEWING` | `RETRY` | Review failed or verdict not pass; `review_feedback` set. |
| `REVIEWING` | `WAITING_*` | Quota, capacity/transient, or timeout/stall during review (`waiting_phase=review`); resume is verification/review, not a new implement. |
| `PASSED` | `MERGING` | Stage integration started. |
| `MERGING` | `DONE` | Branch merged and stage verification passed. |
| `MERGING` | `PASSED` | Stage merge or stage verification failed; integration rolled back to stage checkpoint. |
| `*` (in loop) | `BLOCKED` | Attempt budget exhausted, setup/env failure, or blocked after repeated stalls. |

Tasks already `PASSED` or `DONE` are not re-run when a stage resumes. Sibling tasks can remain `PASSED` while another task is `BLOCKED`.

### Attempts, retries, and model escalation

**Attempt budget** (per task run):

The implement loop continues while `runtime.attempt < max_attempts` **and**, for `ProviderAgentRunner`, `has_implementation_budget` is true. `build_runtime` always installs `ProviderAgentRunner`; that class sets `manages_model_routing = True` even when Codex is disabled.

`max_attempts` from `_defaults`:

- Manual `--model-class` on `run-task` / `rerun-blocker`: **1**.
- Task `max_attempts` if set.
- Else: `len(codex.implementation_sequence) + max(1, len(retry.sequence))`.
- Two further `_defaults` branches exist only when the runner does **not** set `manages_model_routing`: `len(retry.sequence)` if that list is non-empty, otherwise `execution.default_max_attempts` (default 2). Production never takes those branches.

**Live implement-call cap** (`has_implementation_budget`; this is what actually allows another implement call):

- Manual `--model-class`: one Cursor implement call.
- Active provider Codex: `len(codex.implementation_sequence)` Codex implement calls.
- Active provider Cursor: `max(1, len(retry.sequence))` Cursor implement calls.

With default config (`codex.enabled: false`, empty `retry.sequence`), Cursor is the active provider, so the live cap is **one** Cursor implement call, not 2.

**Cursor model class per attempt** (when not using manual override): `retry.sequence[provider_attempt_index]` or `[task.model_class]` if sequence empty.

**Codex implementation** uses `codex.implementation_sequence` entries (model + `reasoning_effort` per index).

**Quota/capacity waits** decrement the current attempt when `decrement_attempt` applies (implement phase), so pauses do not consume retry budget.

**Stalls**: watchdog kills the agent when both CLI output and worktree file activity are silent for `stall_timeout_minutes`. Three stall timeouts for the same phase/provider without successful fallback → `BLOCKED` ("stalled 3 times"). Provider fallback may switch Codex↔Cursor for that phase after stalls.

**Failure context for the next attempt**: implementation prompt includes `last_error` or `review_feedback` from the previous failure.

### BLOCKED and recovery commands

| Situation | Command |
| --- | --- |
| Task blocked after retries (real fresh budget) | `harness rerun-blocker <sprint>` (`--task <id>` if multiple blocked; optional `--model-class`). This is `run-task --fresh-budget`: `RETRY`, `attempt = 0`, and **clears `provider_attempts` and `stall_counts`**. |
| Focused rerun with a real fresh budget | `harness run-task <id> --fresh-budget` (same counter reset as `rerun-blocker`) |
| Plan gap suspected | `harness plan-change propose <sprint> --task <id>` |
| Manual retry (does **not** reset live implement budget) | `harness retry <task>` → `RETRY`, `attempt = 0` only. Leaves `provider_attempts` and `stall_counts`. |
| Unblock after BLOCKED (same as retry) | `harness unblock <task>` → `RETRY`, `attempt = 0` only. Same counters left in place. |
| Quota/capacity pause | `harness resume <sprint>` (also auto-waits when `quota.policy` / `capacity.policy` is `wait`) |
| Stage barrier failed | `harness rerun-blocker <sprint>` (reruns merge + stage checks for `PASSED` tasks in that stage) |
| Inspect failure | `harness failure <sprint>` |

`retry` / `unblock` are not a fresh implementation budget. Production always uses `ProviderAgentRunner`; live implement calls are gated by `has_implementation_budget`. After one Cursor implement with default config (`codex.enabled: false`, empty `retry.sequence`), `retry` then `resume` sees `provider_attempts["cursor"] == 1`, the implement loop runs zero iterations, and the task is `BLOCKED` again. Use `rerun-blocker` or `run-task --fresh-budget` to clear those counters.

## Stage barrier

1. Before a stage runs, the integration checkout must be **clean** (`git status --porcelain` empty; `.harness/` is gitignored).
2. The harness records `sprint.integration_branch` and `sprint.stage.<n>.base_commit` at the current HEAD.
3. Before the stage thread pool starts (and again if queue pickup adds a task to the running stage), `_assert_parallel_safe` requires pairwise non-conflicting `allowed_paths`. Empty `allowed_paths`, or patterns whose prefix is empty after stripping wildcards, count as unknown and conflict. Overlapping or unknown paths raise `RuntimeError` (`Stage N contains tasks with overlapping/unknown allowed_paths`) and **abort the run** — a hard stage-start failure, not a serial fallback. `max_parallel_agents` still caps concurrency among non-conflicting tasks.
4. After the stage thread pool finishes, merge does **not** begin if any outcome is a quota or capacity wait (`quota_paused` / `capacity_paused`). `run_sprint` records the pause and returns; waiting tasks stay `WAITING_*`; passed siblings are not merged. On later resume, queue pickup still uses `merge_in_progress=False`, so queued tasks can still join the current stage. Merge pickup (`merge_in_progress=true`, so new queued tasks cannot join) and `_integrate_stage` run only when every active task in the stage reached `PASSED` with no waits and no blocked failures.
5. **Integration**: for each task, `git merge --no-ff agent/<task>` on the integration branch; then **stage verification** runs on the integration checkout: `verification.stage_commands` plus sprint `stage_verification[stage]`.
6. If stage verification fails or merge fails: **`git reset --hard`** to the stage checkpoint, **`git clean -fd`**, abort merge if needed; tasks revert from `MERGING` to **`PASSED`**; failure recorded as `STAGE_BARRIER`.
7. On success: tasks become **`DONE`**, stage failure record cleared, optional worktree removal unless `keep_successful_worktrees`.

**What makes a stage fail:** overlapping/unknown `allowed_paths` or unmet dependencies or a dirty integration checkout raise `RuntimeError` and abort the run (no merge). A blocked task or a quota/capacity wait returns from `run_sprint` without merging PASSED siblings. Merge or stage-verification failure rolls the integration checkout back to the checkpoint; tasks revert from `MERGING` to `PASSED`; failure recorded as `STAGE_BARRIER`.

`harness rerun-blocker` on `STAGE_BARRIER` requires integration clean, all stage tasks `PASSED` or `DONE`, and re-runs integration for tasks not yet `DONE`.

## Providers and fallback

**Priority**: `providers.priority` (default `codex`, then `cursor`). First enabled provider for the phase wins.

**Codex** (`codex.enabled`, default false): implementation uses `implementation_sequence`; review uses `review_model`; plan repair uses `planner_model`. Availability is probed via CLI/auth. On quota, auth, or model-unavailable failures, Codex is **disabled for that task** (metadata key) and Cursor is used if enabled. On capacity/transient errors, the run **waits** without disabling Codex.

**Cursor** (`cursor.enabled`, default true): `cursor.models` maps **model class** names (e.g. `worker`, `hard_worker`, `reviewer`) to CLI `--model` ids. Implementation uses `--force` and `--trust` only in verified harness worktrees under `.harness/worktrees/`. Review uses `review_model_class` or class name `reviewer`.

**Per-task fallback state** persists in state metadata (e.g. `sprint.task.provider.codex.disabled`). Each new task probes Codex again.

**Manual `--model-class`**: forces Cursor, disables automatic multi-step Codex/Cursor routing for that invocation.

## Quota and capacity

**Quota** (account usage limits, Cursor/Codex pattern lists in config): tasks enter `WAITING_QUOTA`. Failure kind `QUOTA_WAIT`; suggested `harness resume`. On `run`/`resume`, if `quota.policy` is **`wait`**, the harness sleeps (`probe_interval_minutes` or until `quota.reset_at` + `reset_grace_minutes`, with optional runtime override via `harness quota set-reset`), then retries. Policy **`stop`** exits without sleeping. `quota.teardown_environment_while_waiting` tears down per-task Docker when waiting.

**Capacity** (overload, network, transient errors): tasks enter `WAITING_NETWORK`, `WAITING_PROVIDER_BUSY`, `WAITING_PROVIDER_ERROR`, or `WAITING_TIMEOUT`. Failure kind `CAPACITY_WAIT`. **`capacity.policy` `wait`**: sleep `retry_interval_minutes` and resume; **`stop`**: exit. Same optional environment teardown as quota.

**Keep-awake**: nested `KeepAwake` on Windows during run and waits when configured.

**Automatic resume**: only inside an active `harness run` / `harness resume` loop when policy is `wait`. Ctrl+C is safe; state is persisted (`PAUSED_BY_USER` on manifest).

## Review

**When review runs**: task `review: true`, or any changed file matches `paths.review_required` globs after verification.

**Reviewer prompt** includes AGENTS.md, brief, resolved context paths, verification summary, diff against worktree `base_ref`, and optional code-map / UI sections.

**Verdict**: scheduler accepts pass only if agent `ok` and some output line equals exactly `VERDICT: PASS` (see `review_prompt` for required format). Otherwise status `RETRY` and full review output stored in `review_feedback`.

**UI review** (`ui_review.enabled`): extra prompt instructions; Playwright MCP may be auto-approved on Cursor review calls. After review, worktree snapshot compared; reviewer edits fail the review via synthetic `VERDICT: FAIL`.

**Codex vs Cursor** for review follows the same priority/fallback rules as implementation.

**Resume after a review-phase wait**: every `WAITING_*` with `waiting_phase == "review"` (quota, capacity/transient, or `WAITING_TIMEOUT` after a reviewer stall) resumes through `_verify_review_commit` on `run`/`resume`. If worktree HEAD and the recorded verification fingerprint still match, verification is skipped and review continues. If they do not match, verification re-runs, then review. Skip-verification-on-fingerprint is not quota-only.

## Code map

Optional Graphify index (`code_map.command`, default `graphify`).

1. **`ensure_project_map(base_commit)`** (under `.harness/code-map/`): detached source worktree at commit; runs `graphify update . --no-cluster` unless map already built for that commit. Skipped if Graphify CLI unavailable.
2. **`offer_to_worktree`**: copies `graphify-out/` into the task worktree, writes `AGENTS-graphify.md`, adds `graphify-out/` to git exclude if needed.
3. Implementation/review prompts mention the map when offered; agents are told to read `AGENTS-graphify.md` and use `--budget 500` on `graphify query`.

Usage logging can count graphify invocations from agent shell commands.

## Reviewer UI check

When `ui_review.enabled` is true, the review prompt instructs the reviewer to start app commands (`ui_review.start_commands`), open `ui_review.url` with Playwright browser tools when UI behavior may change, and **not modify files**. After `VERDICT`, the reviewer must add `UI_CHECK: done - …` or `UI_CHECK: skipped - …`. UI issues are FAIL findings.

After review, the harness kills processes listening on configured `ui_review.ports` or whose command line references the task worktree (platform-specific discovery), logging to `<task>-ui-review-cleanup.log`.

## Release

After every task in a sprint is **`DONE`**, no pending queue entries, and sprint finalize runs: if `release.commands` is non-empty and release has not run yet, **`ReleaseRunner`** executes commands once on the integration checkout. `release.timeout_minutes` is passed into `Verifier.run_shell_commands` and applies to **each** command, not the whole batch. Success/failure is stored in state metadata; **failed release does not roll back merged code**. Retry with `harness release <sprint>` (requires sprint complete). Empty `release.commands` skips release.

## Adding tasks during a run

**Enqueue**: `harness add-task <sprint> <brief> [--stage auto|current|next]` validates the brief (unique id, non-empty `allowed_paths`, dependencies known, sprint not complete at add time). Files go to `.harness/queue/<id>.md` and sidecar JSON.

**Pickup**: during stage execution and after merge boundaries, `process_pending` accepts or rejects queued briefs:

- **`current`**: join current stage if merge not started, dependencies `DONE`, no `allowed_paths` conflict.
- **`next`**: new stage number with dependency ordering rules.
- **`auto`**: try current, else next.

Accepted tasks move to `.harness/queue/accepted/`, are marked `runtime.added`, bump `plan.revision`, and join the in-memory sprint.

**After sprint completes**: `record_added_tasks_to_repo` copies accepted briefs into the repo, updates `roadmap.yaml`, and commits (requires clean integration checkout).

## Configuration reference

Defaults are from `HarnessConfig.load` when keys are omitted. Legacy keys `execution.require_clean_base`, `code_map.enabled`, `code_map.default_for_tasks`, and codex/capacity legacy pattern keys are ignored or merged for compatibility.

### `execution`

| Key | Default | Meaning |
| --- | --- | --- |
| `max_parallel_agents` | `3` | Max concurrent tasks per stage. |
| `default_max_attempts` | `2` | Fallback in `_defaults` only when the runner does not set `manages_model_routing` and `retry.sequence` is empty. Production always installs `ProviderAgentRunner`, so this is not the live attempt cap; see Task lifecycle. |
| `default_timeout_minutes` | `35` | Agent call hard timeout per task. |
| `base_ref` | `null` | If set, first run requires integration HEAD to match this ref. |
| `keep_successful_worktrees` | `false` | Keep worktree dirs after `DONE`. |
| `stall_timeout_minutes` | `12.0` | Silence watchdog (independent of hard timeout). |
| `watchdog_poll_seconds` | `10.0` | Poll interval for agent process and queue pickup. |
| `keep_awake_during_run` | `true` | Windows keep-awake for run/resume lifecycle. |

### `worktree`

| Key | Default | Meaning |
| --- | --- | --- |
| `setup_commands` | `[]` | Shell commands after worktree creation. |
| `setup_timeout_minutes` | `10` | Per-command timeout for setup. |

### `code_map`

| Key | Default | Meaning |
| --- | --- | --- |
| `command` | `"graphify"` | Graphify CLI invocation. |
| `build_timeout_minutes` | `5` | Timeout for `graphify update`. |

### `cursor`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Allow Cursor agent calls. |
| `command` | `"agent"` | Cursor CLI executable. |
| `output_format` | `"stream-json"` | CLI output format (enables usage parsing). |
| `trust_harness_worktrees` | `true` | Pass `--trust` only for verified `.harness/worktrees` children. |
| `models` | `{}` | Map model class name → Cursor model id (required for declared classes). |

### `codex`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Allow Codex CLI. |
| `command` | `"codex"` | Codex executable. |
| `reasoning_effort` | `"high"` | Default effort when sequence entry is a string. |
| `sandbox` | `"workspace-write"` | Codex sandbox mode. |
| `windows_implementation_sandbox` | `null` | Override sandbox for Codex implement on Windows. |
| `approval_policy` | `"never"` | Codex approvals. |
| `network_access` | `true` | Codex network access. |
| `implementation_sequence` | four profiles (gpt-5.6-sol medium/medium/high, gpt-6-astra medium) | Per-Codex-attempt model and effort. |
| `review_model` | `"gpt-5.6-sol"` | Codex reviewer model. |
| `planner_model` | `"gpt-5.6-sol"` | Codex plan-repair model. |
| `auth_required_substring` | `"Logged in using ChatGPT"` | Auth probe success marker. |
| `short_quota_patterns`, `weekly_quota_patterns`, `quota_patterns`, `busy_patterns`, `network_patterns`, `provider_error_patterns`, `auth_patterns`, `model_unavailable_patterns` | (see `config.py`) | Output classification for waits and fallback. |

### `providers`

| Key | Default | Meaning |
| --- | --- | --- |
| `priority` | `["codex", "cursor"]` | Provider try order. |

### `retry`

| Key | Default | Meaning |
| --- | --- | --- |
| `sequence` | `[]` | Cursor model classes per attempt; empty means repeat task `model_class`. |

### `verification`

| Key | Default | Meaning |
| --- | --- | --- |
| `global_task_commands` | `["git diff --check"]` | Run after every task in worktree. |
| `stage_commands` | `[]` | Run on integration checkout at stage barrier. |
| `env` | `{}` | Default env for verification only (`setdefault`, never overrides task env). |

### `paths`

| Key | Default | Meaning |
| --- | --- | --- |
| `protected` | `[]` | Paths agents must not change unless `allow_protected`. |
| `review_required` | `[]` | Changed paths forcing review even if `review: false`. |

### `context`

| Key | Default | Meaning |
| --- | --- | --- |
| `aliases` | `{}` | Logical names → repo-relative files for task `context`. |

### `environment`

| Key | Default | Meaning |
| --- | --- | --- |
| `docker_enabled` | `false` | Allow `environment: docker` on tasks. |
| `compose_command` | `"docker compose"` | Compose executable. |
| `compose_files` | `["docker-compose.yml"]` | Compose files. |
| `project_name_prefix` | `"harness"` | Compose project name prefix. |
| `services` | `[]` | Compose services to manage. |
| `validate_compose` | `true` | Validate compose config before up. |
| `up_before_agent` | `false` | Start compose before agent. |
| `teardown_on_success` | `true` | Tear down after task success. |
| `teardown_on_blocked` | `true` | Tear down when blocked or waiting (if configured). |
| `remove_volumes` | `true` | Remove volumes on teardown. |
| `command_timeout_minutes` | `10` | Docker command timeout. |
| `env` | `{}` | Extra env for compose commands. |

### `quota`

| Key | Default | Meaning |
| --- | --- | --- |
| `policy` | `"wait"` | `wait` or `stop` when quota exhausted. |
| `reset_at` | `null` | ISO timestamp for quota reset wait. |
| `probe_interval_minutes` | `60.0` | Sleep interval when no reset time. |
| `reset_grace_minutes` | `5.0` | Extra wait after reset time. |
| `keep_awake` | `true` | Keep Windows awake during quota sleep. |
| `teardown_environment_while_waiting` | `true` | Tear down task env while waiting. |
| `exhaustion_patterns` | (see `config.py`) | Cursor quota detection strings. |

### `capacity`

| Key | Default | Meaning |
| --- | --- | --- |
| `policy` | `"wait"` | `wait` or `stop` on capacity/transient waits. |
| `retry_interval_minutes` | `5.0` | Sleep between capacity retries. |
| `keep_awake` | `true` | Keep awake during capacity sleep. |
| `teardown_environment_while_waiting` | `true` | Tear down task env while waiting. |
| `busy_patterns`, `network_patterns`, `provider_error_patterns` | (see `config.py`) | Capacity/transient classification. |

### `plan_repair`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Gates `harness plan-change propose`. When false, that command exits with "Plan repair is disabled in harness.yaml". `harness validate` still runs; if this flag is true and `planner_model_class` is undeclared under `cursor.models`, validate only warns. |
| `planner_model_class` | `"escalation"` | Default model class for `plan-change propose`. |
| `timeout_minutes` | `20` | Planner agent timeout. |

### `ui_review`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Enable UI check instructions and cleanup. |
| `start_commands` | `[]` | Commands reviewer may use to start app. |
| `url` | `null` | URL to open in browser tools. |
| `ports` | `[]` | Ports to monitor/cleanup. |
| `playwright_mcp_package` | `"@playwright/mcp@0.0.84"` | Documented package name for Playwright MCP. |

### `release`

| Key | Default | Meaning |
| --- | --- | --- |
| `commands` | `[]` | Shell commands after sprint completes. |
| `timeout_minutes` | `15` | Timeout applied to **each** release command (`Verifier.run_shell_commands` → `subprocess.run(..., timeout=...)` per command). Three commands can each run for the full 15 minutes. |

### `doctor`

| Key | Default | Meaning |
| --- | --- | --- |
| `required_commands` | `[]` | Commands that must exist. |
| `required_env` | `[]` | Environment variables that must be set. |
| `min_free_disk_gb` | `1.0` | Minimum free disk. |
| `check_cursor_models` | `true` | Validate Cursor model access. |
| `minimum_versions` | `{}` | Tool → minimum version map. |

### Task brief frontmatter keys

| Key | Default | Meaning |
| --- | --- | --- |
| `id` | (required) | Task id (must match roadmap when listed there). |
| `stage` | `1` | Stage number (roadmap row can override). |
| `depends_on` | `[]` | Task ids that must be `DONE` before start. |
| `model_class` | `"worker"` | Cursor model class for implementation. |
| `review_model_class` | `null` | Override reviewer class (else `reviewer`). |
| `max_attempts` | `null` | Cap on `runtime.attempt` in `_defaults`. If unset, that cap is `len(codex.implementation_sequence) + max(1, len(retry.sequence))`; live implement calls still require `has_implementation_budget`. |
| `timeout_minutes` | `null` | Agent timeout (else `default_timeout_minutes`). |
| `review` | `true` | Primary review switch. Independent review runs when `review` is true **or** any changed file matches `paths.review_required` (`review_required = task.review or self.verifier.requires_review(...)`). `paths.review_required` can only force review on; it cannot turn review off. |
| `allow_protected` | `false` | Allow edits under `paths.protected`. |
| `allowed_paths` | (required non-empty for queue) | Glob patterns for permitted edits. |
| `verification` | `[]` | Extra verification commands in worktree. |
| `context` | `[]` | Context refs or aliases resolved for prompts. |
| `environment` | `null` | e.g. `docker` when Docker enabled in config. |

Roadmap sprint keys: `name`, `tasks` (map of id → `{ file, stage, … }`), `stage_verification` (map stage → command list).

## Guarantees

- The integration checkout must be clean before a stage starts and before atomic rollback; stage failure restores that checkpoint with `reset --hard` and `clean -fd`.
- Agents run only inside task worktrees; the harness commits with message `harness: <task-id>` after verification/review.
- Verification rejects changes outside `allowed_paths` and (unless `allow_protected`) any `paths.protected` match.
- A stage merges only after all its active tasks reach `PASSED` (no blocked tasks and no quota/capacity waits); merge runs stage verification on the integration checkout and rolls back on failure.
- Quota and capacity pauses preserve task state and do not consume retry attempts (implement-phase waits decrement attempt when pausing).
- Failed **`harness release`** does not undo merged sprint commits.
- Open plan-change requests block `run`/`resume` until applied or rejected.
- Sprint integration branch and initial `base_ref` are pinned for the lifetime of a sprint run.

## Known gaps

- **`READY`** is a defined status but no live transition assigns it; scheduling uses `PENDING` and direct moves into `IMPLEMENTING` / waiting states.
- **Detached HEAD** as integration base is rejected (`current_branch` requires a named branch).
- **Codex `implementation_sequence`** default in code includes four entries; starter `harness init` YAML may omit them and rely on code defaults.
- **`execution.default_max_attempts`** is loaded and defaults to 2, but production never uses it: `build_runtime` always installs `ProviderAgentRunner`, whose `manages_model_routing` skips that `_defaults` branch. Live implement calls are gated by `has_implementation_budget` (one Cursor call with default `codex.enabled: false` and empty `retry.sequence`).
