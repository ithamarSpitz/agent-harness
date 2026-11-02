# Task and stage lifecycle

This document describes task statuses, transitions, retries, blocking, recovery commands, and the stage integration barrier. Behavior matches the implementation under `harness/` today.

## Task lifecycle

### Status values

| Status | Meaning |
| --- | --- |
| `PENDING` | Not started (default for a new task). |
| `READY` | Defined in the enum and accepted when loading legacy persisted state; the scheduler does not assign this status during normal runs. |
| `IMPLEMENTING` | Implementation agent call in progress. |
| `CHECKING` | Task verification (diff scope, protected paths, commands) running. |
| `REVIEWING` | Independent reviewer agent running. |
| `PASSED` | Task checks and review (if required) passed; commit recorded in the worktree; waiting for stage merge. |
| `MERGING` | Stage integration in progress for this task. |
| `RETRY` | Last attempt failed; will run again if attempt and provider budgets allow when the task is scheduled. |
| `DONE` | Task branch merged and stage barrier succeeded. |
| `BLOCKED` | Attempt or provider budget exhausted, setup/env failure, or blocked after repeated stalls without fallback; needs recovery. |
| `WAITING_QUOTA` | Provider account quota exhausted; worktree and state preserved; implement-phase pauses can decrement `attempt` so the pause does not consume a retry slot. |
| `WAITING_NETWORK` | Transient network failure; same preservation rules as other capacity waits. |
| `WAITING_PROVIDER_BUSY` | Provider capacity or overload; same preservation rules. |
| `WAITING_PROVIDER_ERROR` | Other transient provider errors. Legacy persisted name `WAITING_FOR_CAPACITY` maps here. |
| `WAITING_TIMEOUT` | Hard timeout or stall watchdog fired; may trigger provider fallback before blocking. |

Legacy persisted names `RUNNING`, `VERIFYING`, `VERIFIED`, and `WAITING_FOR_QUOTA` map to the statuses above when state is loaded.

### Transitions

| From | To | Cause |
| --- | --- | --- |
| `PENDING` / `RETRY` | `IMPLEMENTING` | Task run starts a new implement loop iteration; `attempt` incremented first. |
| Any `WAITING_*` with `waiting_phase != "review"` | `IMPLEMENTING` | Implement-phase resume on `run` / `resume`; `attempt` incremented. Quota/capacity pauses during implement use `decrement_attempt=True`, so the increment restores the same attempt number. |
| Any `WAITING_*` with `waiting_phase == "review"` | `CHECKING` or `REVIEWING` | `run` / `resume` calls `_verify_review_commit` with `resume_review=True`. No new implement call. If worktree `HEAD` and the recorded verification fingerprint still match, verification is skipped and review continues (`REVIEWING`). Otherwise verification runs (`CHECKING`), then review. Applies to every review-phase wait, including `WAITING_TIMEOUT` after a reviewer stall. |
| `IMPLEMENTING` | `CHECKING` | Implementation agent returned success. |
| `IMPLEMENTING` | `RETRY` | Implementation agent failed (non-wait). |
| `IMPLEMENTING` | `WAITING_*` | Quota or capacity/transient wait during implement (`waiting_phase=implement`). |
| `CHECKING` | `REVIEWING` | Verification passed and review required. |
| `CHECKING` | `PASSED` | Verification passed, review not required, commit succeeded. |
| `CHECKING` | `RETRY` | Verification failed. |
| `REVIEWING` | `PASSED` | Review agent `ok` and some output line equals exactly `VERDICT: PASS`; commit succeeded. |
| `REVIEWING` | `RETRY` | Review failed or verdict not pass; `review_feedback` set. |
| `REVIEWING` | `WAITING_*` | Quota, capacity/transient, or timeout/stall during review (`waiting_phase=review`); resume is verification/review only, never a new implement call. |
| `PASSED` | `MERGING` | Stage integration started. |
| `MERGING` | `DONE` | Branch merged and stage verification passed. |
| `MERGING` | `PASSED` | Stage merge or stage verification failed; integration rolled back to stage checkpoint. |
| (loop exit) | `BLOCKED` | Attempt budget exhausted, worktree setup or environment preparation failed, or blocked after three stall timeouts for the same phase/provider without successful fallback. |

Tasks already `PASSED` or `DONE` are not re-implemented when a stage runs again; `_run_task` returns success immediately. Sibling tasks can stay `PASSED` while another task is `BLOCKED` or waiting.

Review-phase resume never sets `IMPLEMENTING`. Implement-phase resume always re-enters the implement loop.

### Attempts, retries, and model escalation

**Loop condition:** `_run_task` continues while `runtime.attempt < max_attempts` and, when the runner sets `manages_model_routing`, while `has_implementation_budget` is true. `build_runtime` always installs `ProviderAgentRunner`, which sets `manages_model_routing = True`.

**`max_attempts` (`_defaults`):**

| Condition | Value |
| --- | --- |
| `run-task` / `rerun-blocker` with `--model-class` | `1` |
| Task brief `max_attempts` set | That value |
| `ProviderAgentRunner` (normal production) | `len(codex.implementation_sequence) + max(1, len(retry.sequence))` |
| Runner without `manages_model_routing` and non-empty `retry.sequence` | `len(retry.sequence)` |
| Runner without `manages_model_routing` and empty `retry.sequence` | `execution.default_max_attempts` (default `2`) |

The last two rows are not used in production because `ProviderAgentRunner` is always installed.

**Live implement-call cap (`has_implementation_budget`):**

| Condition | Cap |
| --- | --- |
| Manual `--model-class` | One Cursor implement call (`provider_attempts["cursor"] < 1`). |
| Active provider Codex | `len(codex.implementation_sequence)` Codex implement calls. |
| Active provider Cursor | `max(1, len(retry.sequence))` Cursor implement calls. |

With default config (`codex.enabled: false`, empty `retry.sequence`), Cursor is active, so the live cap is **one** Cursor implement call even though `max_attempts` from `_defaults` may be larger.

**Cursor model class per implement call** (no manual override): `retry.sequence[provider_attempt_index]`, or `[task.model_class]` when `retry.sequence` is empty.

**Codex implement calls** use `codex.implementation_sequence` entries (model and optional `reasoning_effort` per index).

**Quota waits** during implement decrement `attempt` when `attempt > 0` before entering `WAITING_QUOTA`. **Capacity waits** during implement do the same when `decrement_attempt` is true. Review-phase waits use `decrement_attempt=False`.

**Stall watchdog:**

| Provider | Activity counted toward stall timeout |
| --- | --- |
| Cursor | CLI stdout activity **or** worktree file activity. Each poll runs `git ls-files -co --exclude-standard` (tracked and non-ignored untracked files) and treats a change in file count, mtime sum, or total size as activity. |
| Codex | CLI stdout and stderr activity only (no worktree file polling). |

Both use `execution.stall_timeout_minutes` (default `12`) and `execution.watchdog_poll_seconds` (default `10`). Hard task timeout is separate (`timeout_minutes` on the brief or `execution.default_timeout_minutes`, default `35`).

On `waiting_cause == "timeout"`, the scheduler increments `stall_counts["{phase}:{provider}"]`. At **3** for the same key, it tries `fallback_after_stalls` (disable that provider for the phase and switch Codex↔Cursor when the other is available). If fallback fails, the task becomes `BLOCKED` with message `stalled 3 times`.

**Next attempt context:** the implement prompt receives `previous_failure` from `last_error` or `review_feedback`.

### Related config defaults

| Key | Default | Role in lifecycle |
| --- | --- | --- |
| `execution.default_max_attempts` | `2` | Fallback `max_attempts` only when the runner does not manage model routing. |
| `execution.default_timeout_minutes` | `35` | Task agent timeout when the brief omits `timeout_minutes`. |
| `execution.max_parallel_agents` | `3` | Stage thread pool size cap. |
| `execution.stall_timeout_minutes` | `12` | Stall watchdog silence threshold. |
| `execution.watchdog_poll_seconds` | `10` | Poll interval for timeouts, stalls, and stage queue pickup. |
| `execution.keep_successful_worktrees` | `false` | When false, successful stage integration removes task worktrees and deletes `agent/<task-id>` branches. |
| `retry.sequence` | `[]` | Cursor model-class escalation list for implement calls. |
| `codex.enabled` | `false` | Whether Codex is considered before Cursor in provider priority. |
| `codex.implementation_sequence` | four default Codex profiles | Codex implement-call sequence when Codex is active. |
| `verification.stage_commands` | `[]` | Extra commands run at stage barrier **before** per-sprint `stage_verification`. |

### BLOCKED and recovery commands

| Command | When to use | What it resets or does |
| --- | --- | --- |
| `harness rerun-blocker <sprint>` | Active failure `TASK_BLOCKED` or `STAGE_BARRIER` | **`TASK_BLOCKED`:** runs `run-task` on the blocked task with `--fresh-budget`: `RETRY`, `attempt = 0`, clears `provider_attempts`, `stall_counts`, `review_feedback`, and `commit`; optional `--task` when several are blocked; optional `--model-class`. **`STAGE_BARRIER`:** requires clean integration checkout and all stage tasks `PASSED` or `DONE`; re-runs merge and stage verification for tasks not yet `DONE`. |
| `harness run-task <id> --fresh-budget` | Focused rerun with a new implement budget | Same counter reset as `rerun-blocker` for a task (`RETRY`, `attempt = 0`, empty `provider_attempts` and `stall_counts`, clears `review_feedback` and `commit`). Refuses `DONE` tasks. |
| `harness retry <task>` | Manual retry without clearing provider counters | Sets `RETRY`, `attempt = 0`, clears `review_feedback`; leaves `provider_attempts` and `stall_counts`. Does not require `BLOCKED`. |
| `harness unblock <task>` | Task is `BLOCKED` | Same as `retry` when status is `BLOCKED`: `RETRY`, `attempt = 0`, clears `review_feedback`; leaves `provider_attempts` and `stall_counts`. |
| `harness reset-task <task>` | Discard task progress and worktree | Removes worktree and `agent/<task-id>` branch when present; `state.reset_task` to a fresh runtime (keeps `added` and `brief_path`); clears that task on the sprint failure record. Refuses `DONE`. |
| `harness resume <sprint>` | Quota or capacity pause, or continue after partial stage | Retries `run_sprint`; with `quota.policy` or `capacity.policy` `wait`, sleeps then loops inside `run` / `resume`. |
| `harness failure <sprint>` | Inspect active failure | Read-only render of `.harness/failures` record (suggested next command). |
| `harness plan-change propose <sprint> --task <id>` | Suspected plan gap | Starts plan-repair workflow; does not by itself reset attempt or provider counters. |

Failure kinds tied to lifecycle: `TASK_BLOCKED`, `STAGE_BARRIER`, `QUOTA_WAIT`, `CAPACITY_WAIT`.

## Stage barrier

A **stage** is the integer on each task. The scheduler runs the lowest stage that still has tasks not `DONE`, then integrates that stage before opening the next.

### Preconditions and checkpoint

1. Integration checkout at `--root` must be **clean** (`git status --porcelain` empty; `.harness/` is gitignored).
2. Every non-`DONE` task in the stage must have dependencies `DONE`.
3. Before the thread pool starts (and again when queue pickup adds a task to the running stage), `_assert_parallel_safe` requires pairwise non-conflicting `allowed_paths`. Empty `allowed_paths`, or patterns whose prefix is empty after stripping wildcards, count as unknown and conflict with anything. Overlap or unknown paths raise `RuntimeError` (`Stage N contains tasks with overlapping/unknown allowed_paths`) and **abort the run** — no merge, no serial fallback.
4. The harness records `sprint.integration_branch` and `sprint.stage.<n>.base_commit` at the current HEAD. Worktrees for new tasks are created from that commit.

`execution.max_parallel_agents` caps how many tasks run concurrently among tasks that passed the safety check.

### When tasks finish the stage thread pool

Outcomes are classified as:

| Outcome | Effect on merge |
| --- | --- |
| Hard failure (`BLOCKED`, verification failure without wait, etc.) | `TASK_BLOCKED` failure recorded; `run_sprint` returns false; **`PASSED` siblings are not merged**; resume does not re-run already `PASSED` tasks. |
| `quota_paused` | `QUOTA_WAIT` failure; tasks stay `WAITING_QUOTA`; **merge does not start**; `PASSED` siblings stay unmerged. |
| `capacity_paused` | `CAPACITY_WAIT` failure; tasks stay in the matching `WAITING_*` status; **merge does not start**; `PASSED` siblings stay unmerged. |
| All tasks reach `PASSED` (including those that were already `PASSED` and were no-ops in the pool) | Queue pickup runs with `merge_in_progress=True` (queued tasks cannot join this stage); integration proceeds. |

A stage **does not start merging** while any of its tasks ended the pool pass in a quota or capacity wait.

**Waiting tasks** keep worktrees and runtime metadata (`waiting_phase`, `waiting_provider`, etc.). On `resume`, they continue from implement or verification/review per the transitions above. Tasks still waiting do not block merge by themselves once other outcomes are all success — but while any task is still waiting from the **same** pool pass, merge is skipped entirely.

### Integration and rollback

1. Each task in the stage (the unfinished list for that run) is set to `MERGING`.
2. For each task, `git merge --no-ff agent/<task-id>` on the integration branch (merge message `harness: integrate <task-id>`).
3. **Stage verification** on the integration checkout: `verification.stage_commands` plus sprint `stage_verification[stage]`. If commands succeed but the tree is dirty afterward, verification fails.
4. **Failure:** `git merge --abort` if needed, `git reset --hard` to `sprint.stage.<n>.base_commit`, `git clean -fd`; tasks revert from `MERGING` to **`PASSED`**; failure kind **`STAGE_BARRIER`** with suggested `harness rerun-blocker <sprint>`.
5. **Success:** tasks become **`DONE`**, stage failure cleared, optional worktree removal unless `execution.keep_successful_worktrees` is true.

### What makes a stage fail (summary)

| Failure mode | Merge attempted? | Integration rollback? | Task statuses after |
| --- | --- | --- | --- |
| Overlapping/unknown `allowed_paths` | No (run aborts) | N/A | Unchanged |
| Dependencies not `DONE` or dirty integration before stage | No (run aborts) | N/A | Unchanged |
| Blocked task in stage pool | No | N/A | Blocked task `BLOCKED`; others may be `PASSED` |
| Quota or capacity wait in stage pool | No | N/A | Waiters `WAITING_*`; others may be `PASSED` |
| Merge or stage verification error | Yes | Yes, to stage checkpoint | Revert to `PASSED` |

`harness rerun-blocker` on `STAGE_BARRIER` requires a clean integration checkout, all stage tasks `PASSED` or `DONE`, and re-integrates tasks not yet `DONE`.

## Known gaps

- **`harness retry` and `harness unblock`** print messages about a “fresh retry budget” but only set `RETRY`, `attempt = 0`, and clear `review_feedback`. They do **not** reset `provider_attempts` or `stall_counts`. With default Cursor-only config, one successful implement call sets `provider_attempts["cursor"]` to `1`, so a later `retry` plus `resume` can run zero implement iterations and land in `BLOCKED` again. A real fresh implement budget requires `harness run-task <id> --fresh-budget` or `harness rerun-blocker` for `TASK_BLOCKED`.
- **`execution.stall_timeout_minutes` comment in `config.py`** says the watchdog waits for silence on “both CLI output and repository file activity”; that matches **Cursor** only. **Codex** stall detection uses CLI output alone.
