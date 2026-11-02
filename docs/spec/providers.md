# Providers

This document describes how the harness chooses Codex vs Cursor, switches providers, classifies failures, and pauses for quota or capacity. `SPEC.md` links the other specification parts.

## Providers and fallback

### Priority and enable flags

| Config | Default | Role |
| --- | --- | --- |
| `providers.priority` | `["codex", "cursor"]` | First **enabled** entry wins for each phase (`implement`, `review`, `plan_repair`). |
| `codex.enabled` | `false` | Codex CLI integration. |
| `cursor.enabled` | `true` | Cursor CLI integration. |

If the priority list names a disabled provider, the harness skips it and tries the next. If nothing is enabled, agent calls fail with an auth-style error.

### What each provider runs

| Phase | Codex | Cursor |
| --- | --- | --- |
| Implementation | `codex.implementation_sequence` entry by Codex attempt index (model + optional `reasoning_effort` per entry) | `cursor.models[<class>]` where class comes from `retry.sequence` or the task’s `model_class` |
| Review | `codex.review_model` | `cursor.models[task.review_model_class or "reviewer"]`, `--mode=ask` |
| Plan repair | `codex.planner_model` | `cursor.models[planner_model_class]` (CLI default class `escalation` when not overridden) |

**Codex probe (availability):** `codex --version`, then `codex login status`. Auth is OK when exit code is 0 and output contains `codex.auth_required_substring` (default `Logged in using ChatGPT`). Result is cached until `refresh=True`.

**Cursor headless worktrees:** For a verified harness worktree under `.harness/worktrees/` (registered Git worktree), implementation calls add `--trust` and `--force` when `cursor.trust_harness_worktrees` is true (default). Review and plan repair do not get `--force`.

### Per-task and per-phase routing

`ProviderAgentRunner` (`manages_model_routing = true` in production) owns provider choice and attempt indices in `runtime.provider_attempts` (`codex`, `cursor`).

**Task-wide Codex disable** (metadata `{sprint}.{task_id}.provider.codex.disabled`): set when Codex hits quota, auth, model-unavailable, CLI/auth probe failure, or integration error **and** Cursor fallback is enabled. The **same** agent call then continues on Cursor without entering a waiting status. Value is a reason string (for example `quota:5h`, `auth`, `auth_or_cli_unavailable`).

**Task-wide Cursor disable** (`{sprint}.{task_id}.provider.cursor.disabled`): reserved; cleared with Codex disable on a **new** sprint run manifest, not on `resume`.

**Per-phase provider disable** (`{sprint}.{task_id}.provider.{provider}.disabled.{phase}`): after **three** stall/timeout events for the same phase and provider, `fallback_after_stalls` may disable that provider for that phase only and record a switch to the other provider (Codex→Cursor if Cursor enabled; Cursor→Codex only if a fresh Codex availability probe succeeds). Unlike Codex quota/auth fallback, this is **not** a same-call continue: `_handle_agent_wait` still pauses with `WAITING_TIMEOUT`, `waiting_provider` stays the stalled provider, and the alternate provider runs only on the next `harness run` / `harness resume` (with `capacity.policy: wait`, after `retry_interval_minutes`).

Each **new** run manifest clears task-wide Codex/Cursor disable keys so every task probes Codex again. **Resume** preserves prior fallback.

### Codex vs Cursor on failure (same call)

| Failure class | Codex behavior when `cursor.enabled` | Cursor behavior |
| --- | --- | --- |
| Quota (output matches Codex quota patterns) | Disable Codex for task; **continue on Cursor in the same call** — no `WAITING_QUOTA` | Enters quota wait path (see below) |
| Auth / model unavailable | Disable Codex for task; continue on Cursor | Treated as a normal failed call (retry loop), not automatic provider switch |
| Capacity / network / provider error / timeout | Return result; scheduler may **wait**; Codex **not** disabled | Return result; scheduler may **wait** |
| Codex unavailable with Cursor disabled | Return failure (`quota_exhausted`, `auth_failure`, etc.) | N/A |

If Codex quota/auth/model failure occurs and Cursor is **disabled**, the harness does **not** fall back; quota exhaustion can still lead to `WAITING_QUOTA` when the returned result has `quota_exhausted`.

### Implementation attempt budget

With `ProviderAgentRunner`, the implement loop continues while `runtime.attempt < max_attempts` **and** `has_implementation_budget` is true.

**Outer cap** (`Scheduler._defaults` → loop `max_attempts`):

| Condition | Cap |
| --- | --- |
| CLI `--model-class` | **1** |
| Task sets `max_attempts` | that integer (not 1) |
| Otherwise | `max_implementation_attempts()` = `len(codex.implementation_sequence) + max(1, len(retry.sequence))` |

`max_implementation_attempts()` always returns that sum. It does not read `task.max_attempts` or `--model-class`. The implement loop also requires `has_implementation_budget` for the **active** provider, so the Cursor half of that sum is not used while Codex is still preferred.

**Live implement-call cap** (`has_implementation_budget`):

- `--model-class`: one Cursor call (`provider_attempts["cursor"] < 1`). Not `len(retry.sequence)`.
- Active provider Codex: only `codex.implementation_sequence`. Ordinary failures consume one Codex entry and stay on Codex. Sequence exhaustion does **not** fall through to Cursor: if Codex is still enabled, budget is false and the task is **blocked**. Cursor is never started on that path.
- Active provider Cursor (Codex disabled for the task, or Codex not enabled / not preferred — **not** the `--model-class` branch above): up to `max(1, len(retry.sequence))` Cursor-side attempts. Model class uses effective sequence `retry.sequence` if non-empty, otherwise `[task.model_class]`; index `i` (from `provider_attempts["cursor"]`) picks `sequence[min(i, len(sequence) - 1)]`. With default empty `retry.sequence`, that is one attempt at the task’s `model_class`.

Without provider routing (`manages_model_routing` false), `_defaults` uses `len(retry.sequence)` if that list is non-empty, otherwise `execution.default_max_attempts` (default `2`), and per-attempt `model_class` from the task. Production always installs `ProviderAgentRunner`.

**Manual `--model-class`** (on `harness run-task`, `harness rerun-blocker`, etc.): forces **Cursor** only, single attempt, no automatic Codex/Cursor sequence for that invocation.

### Stall fallback vs block

Watchdog stall or hard timeout sets `waiting_cause` `timeout` → task status `WAITING_TIMEOUT`. On implement phase, the harness decrements `attempt` when pausing (same slot reused on resume). After **three** timeouts for the same `phase:provider`, it tries `fallback_after_stalls`; when that succeeds, the per-phase disable is set but the task still enters `_pause_for_capacity` with `WAITING_TIMEOUT` (not an in-call handoff to the other provider). Resume or a later `run`/`resume` loop iteration retries on the alternate provider; `waiting_provider` remains the provider that timed out. If `fallback_after_stalls` fails, status `BLOCKED` with `"stalled 3 times"`.

### Legacy persisted task statuses

| Stored name | Maps to |
| --- | --- |
| `WAITING_FOR_QUOTA` | `WAITING_QUOTA` |
| `WAITING_FOR_CAPACITY` | `WAITING_PROVIDER_ERROR` |

## Quota and capacity

Account **quota** and transient **capacity** are separate. The scheduler records `WAITING_*` only for **implement** and **review** agent calls. Plan repair uses the same providers but does not enter these statuses (see Known gaps). Quota pauses do not consume the task retry counter (implement pauses decrement `attempt` before saving so the next loop iteration reuses the slot). Review-phase waits do not decrement `attempt`.

### Waiting statuses

| Status | Meaning | Typical `waiting_phase` |
| --- | --- | --- |
| `WAITING_QUOTA` | Account / included usage exhausted | `implement` or `review` |
| `WAITING_NETWORK` | Network-style failure | either |
| `WAITING_PROVIDER_BUSY` | Overload / `resource_exhausted`-style capacity | either |
| `WAITING_PROVIDER_ERROR` | Other transient provider/server errors | either |
| `WAITING_TIMEOUT` | Hard timeout or stall watchdog | either |

Runtime fields: `waiting_phase` (`implement` | `review`), `waiting_provider` (`codex` | `cursor`). Sprint-level meta `{sprint_id}.run_status` mirrors the pause for manifests.

### Cause detection

**Codex** classifies **diagnostics** (stderr + parsed events) with `codex.*_patterns` in order: short quota → weekly quota → general quota → auth → model unavailable → (if timed out/stalled) timeout → network → busy → provider error. After that list, two defaults still force `provider_error`: non-zero exit with no match, and exit 0 with no final agent message and no match. Quota kinds set `quota_exhausted`; capacity kinds set `waiting_cause` (`network`, `provider_busy`, `provider_error`, `timeout`). Unmatched Codex failures therefore enter `WAITING_PROVIDER_ERROR`, not a normal retry.

**Cursor** classifies merged output + stream diagnostics:

1. If the call failed and text matches `quota.exhaustion_patterns` → `quota_exhausted` (not capacity).
2. Else if failed and not quota: match `capacity.network_patterns`, then `capacity.busy_patterns`, then `capacity.provider_error_patterns` → `waiting_cause`.
3. If the process **timed out** or **stalled**, `waiting_cause` is forced to `timeout` (overrides pattern matches).
4. If the call failed and none of those match, Cursor does **not** default to `provider_error`: `waiting_cause` stays unset and the scheduler treats it as a normal `RETRY`.

| `waiting_cause` | Task status |
| --- | --- |
| (quota) | `WAITING_QUOTA` |
| `network` | `WAITING_NETWORK` |
| `provider_busy` | `WAITING_PROVIDER_BUSY` |
| `provider_error` | `WAITING_PROVIDER_ERROR` |
| `timeout` | `WAITING_TIMEOUT` |

**Important:** `WAITING_QUOTA` is recorded only when the **agent result** has `quota_exhausted` — in practice **Cursor** quota (or Codex quota when Cursor fallback is disabled). Codex quota with Cursor enabled disables Codex and retries on Cursor in the same call instead of waiting.

### Failure records and exit codes

| Kind | When | Suggested command |
| --- | --- | --- |
| `QUOTA_WAIT` | Stage stopped for quota | `harness resume <sprint>` |
| `CAPACITY_WAIT` | Stage stopped for capacity/transient/timeout wait | `harness resume <sprint>` |

`harness run` / `harness resume` with `quota.policy` / `capacity.policy` **`stop`** exits without sleeping (quota → exit code **4**, capacity → **5**).

### Policies, sleep, and probes

| Config | Default | Quota | Capacity |
| --- | --- | --- | --- |
| `policy` | `"wait"` | `wait` \| `stop` | `wait` \| `stop` |
| Sleep duration | — | `probe_interval_minutes` (**60**) or until `reset_at` + `reset_grace_minutes` (**5**) | `retry_interval_minutes` (**5**) |
| `keep_awake` | `true` | Windows keep-awake during quota sleep | Same during capacity sleep |
| `teardown_environment_while_waiting` | `true` | Tear down per-task Docker when pausing | Same |

**Reset time:** `quota.reset_at` in `harness.yaml`, or runtime override in state meta `quota.reset_at_override` (set/cleared via CLI). `QuotaManager.configured_reset_at()` prefers the override, then yaml, else unknown.

**Automatic resume:** Only inside an active `harness run` or `harness resume` loop when policy is `wait`: after sleep, the loop calls `run_sprint` again. Ctrl+C is safe; manifest may show `PAUSED_BY_USER`. There is no background daemon.

**Keep-awake nesting:** An outer `KeepAwake` wraps the whole run (`execution.keep_awake_during_run`, default true). Quota/capacity waits may nest another `KeepAwake` on Windows.

### What resumes after a wait

| Prior state | `waiting_phase` | On next `run` / `resume` |
| --- | --- | --- |
| Any `WAITING_*` | `review` | `_verify_review_commit` with `resume_review=True`; may skip verification if worktree HEAD and verification fingerprint unchanged, then continue review |
| Any `WAITING_*` | `implement` | Normal implement loop; `attempt` was decremented on pause so the slot is reused |
| (not waiting) | — | Fresh implement attempt or verification as usual |

Review-phase capacity/quota/timeout waits do **not** trigger another implement call before verification/review.

### `harness quota` commands

| Command | Action |
| --- | --- |
| `harness quota status` | Prints quota `policy`, effective `reset_at` and source (`runtime override`, `harness.yaml`, or `unknown`), waiting task ids, plus capacity policy, retry interval, and capacity waiting tasks |
| `harness quota set-reset <ISO-8601>` | Stores normalized timestamp in `quota.reset_at_override` |
| `harness quota clear-reset` | Clears override; yaml/unknown reset applies again |

### Pattern lists (defaults in `harness/config.py`)

**Cursor quota:** `quota.exhaustion_patterns` (for example `usage limit reached`, `quota exceeded`, `no usage remaining`).

**Codex quota:** `codex.short_quota_patterns`, `codex.weekly_quota_patterns`, `codex.quota_patterns` (separate scopes; metadata `quota_scope` may be `5h`, `weekly`, or `unspecified`).

**Capacity (Cursor):** `capacity.busy_patterns`, `capacity.network_patterns`, `capacity.provider_error_patterns`. Legacy yaml keys `capacity_patterns` / `transient_patterns` merge into busy/provider lists on load.

**Capacity (Codex):** `codex.busy_patterns`, `codex.network_patterns`, `codex.provider_error_patterns`.

Codex also uses `codex.auth_patterns` and `codex.model_unavailable_patterns` for fallback (not waiting statuses).

## Known gaps

- Persisted status `WAITING_FOR_CAPACITY` maps to `WAITING_PROVIDER_ERROR`, not to `WAITING_PROVIDER_BUSY`, even when the original failure was overload-shaped.
- Cursor pattern classification runs before the timeout override; a timed-out call is always treated as `WAITING_TIMEOUT` regardless of network-like output in the log.
- Plan repair (`PlanChangeManager.propose` via `harness plan-change propose`) uses the same Codex/Cursor routing as the table above, but `quota_exhausted`, `capacity_exhausted`, and other non-ok results raise `RuntimeError`. They do not set `WAITING_*`, sleep, or resume. The wait statuses in this document apply to implement and review only.
