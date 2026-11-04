# Providers

This document describes how the harness chooses between Antigravity (`agy`), Codex, Cursor and Claude Code, switches providers, classifies failures, and pauses for quota or capacity. `SPEC.md` links the other specification parts.

## Providers and fallback

### Priority and enable flags

| Config | Default | Role |
| --- | --- | --- |
| `providers.priority` | `["codex", "cursor"]` | Base order for headroom routing (see below). Enabled providers omitted from the list are appended after it. Add `"agy"` or `"claude"` to place them explicitly. |
| `codex.enabled` | `false` | Codex CLI integration. |
| `cursor.enabled` | `true` | Cursor CLI integration. |
| `claude.enabled` | `false` | Claude Code CLI integration. |
| `agy.enabled` | `false` | Antigravity CLI integration. The harness invokes the configured `agy` command and uses the user's existing login. |

Disabled providers are never chosen. If nothing is enabled or usable, agent calls fail with an auth-style error or the scheduler follows its wait behavior (see "Headroom routing").

### What each provider runs

| Phase | Antigravity (`agy`) | Codex | Cursor | Claude Code |
| --- | --- | --- | --- | --- |
| Implementation | `agy.implementation_sequence` entry by agy attempt index (default Gemini Flash Medium, Gemini Flash High, then Claude Opus Thinking/high); an explicit escalation class selects `agy.escalation_model` / `agy.escalation_effort` | `codex.implementation_sequence` entry by Codex attempt index (model + optional `reasoning_effort` per entry) | `cursor.models[<class>]` where class comes from `retry.sequence` or the task’s `model_class` | `claude.implementation_sequence` entry by Claude attempt index (default `sonnet/medium`, `sonnet/high`, `opus/high`); an explicit escalation model class selects `claude.escalation_model` / `claude.escalation_effort` |
| Review | `agy.review_model` / `agy.review_effort` (default Claude Sonnet 4.6/high), with `--mode plan` | `codex.review_model` | `cursor.models[task.review_model_class or "reviewer"]`, `--mode=ask` | `claude.review_model` / `claude.review_effort` (default `sonnet` / `high`) |
| Plan repair | Review profile, or escalation profile for an explicit escalation class, with `--mode plan`; agy participates in plan-repair headroom routing only on the routing path enabled by `claude.enabled` | `codex.planner_model` | `cursor.models[planner_model_class]` (CLI default class `escalation` when not overridden) | Claude review profile; with `claude.enabled`, plan repair uses headroom routing, otherwise it keeps the legacy selection path |

Claude runs non-interactively with `stream-json`, the selected model and effort, and `claude.permission_mode` (default `bypassPermissions`).

Antigravity 1.5.0 runs non-interactively with `-p=`, a single NDJSON user message on stdin, `--input-format stream-json --output-format stream-json`, the selected model/effort, and per-call `--settings`. Implementation uses `--mode accept-edits`; review and plan repair use `--mode plan`. The harness closes stdin after writing the message and relies on its own agent timeout rather than `--print-timeout`. It logs `agy --version` once per runner and records response token fields from the final result.

**Agy quota pools and ladder.** `/usage` exposes two weekly pools. The default `agy.pools` maps `gemini-` models to `gemini-weekly` and `claude-` / `gpt-oss-` models to `3p-weekly`. Routing evaluates the pool needed by the current call, not the provider's unrelated pool. If an implementation call exhausts one pool, the runner can immediately skip later ladder entries in that pool and try the next configured entry from another pool without spending another outer attempt. A model used by the implementation, review or escalation profiles must match exactly one configured pool.

The models reported by the installed CLI for S10 are `gemini-3.8-flash-high`, `gemini-3.8-flash-medium`, `gemini-3.8-flash-low`, `claude-sonnet-4-6`, `claude-opus-4-6-thinking`, and `gpt-oss-120b-medium`. The default ladder uses the models shown in the phase table; configured alternatives still have to match exactly one pool.

**Agy permissions and denied actions.** Headless agy cannot ask for permission. Implementation uses `accept-edits`, while shell access is granted only through session-only `--settings '{"permissions":{"allow":[...]}}'`; the harness never saves settings and never uses `--dangerously-skip-permissions`. It builds exact `command(...)` rules from task verification commands, `agy.base_allow`, and exact base-ref diff commands. Verification command chains are split at unquoted `&&`, `|` and `;`. Agy 1.5.0 does not match wildcard or `regex:` command rules, so known Git defaults are expanded and unsupported wildcard, regex and Graphify rules are dropped with a warning. A result is successful only with process exit 0, final status `SUCCESS`, and an empty `denied_actions` list. Any denied action fails the call even though agy may exit 0 and report `SUCCESS`; the error lists its display name and action.

**Agy tools and UI checks.** Agy 1.5.0 reads MCP servers only from saved user configuration and has no per-call MCP setting. The harness never edits that configuration, never offers the code map to agy, and never adds Playwright/MCP setup or UI-check instructions to agy implementation or review calls. `AgyAgentRunner.supports_ui_review` is therefore false. The provider wrapper still performs its ordinary post-call UI process/port cleanup when project UI review is enabled.

**Claude and Playwright:** For implementation and review, Claude receives a strict temporary MCP configuration. With `ui_review.enabled` it contains only the configured Playwright MCP server (`npx` with `ui_review.playwright_mcp_package`); otherwise it is empty so user MCP servers are not loaded. Plan repair does not receive this configuration.

**Codex probe (availability):** `codex --version`, then `codex login status`. Auth is OK when exit code is 0 and output contains `codex.auth_required_substring` (default `Logged in using ChatGPT`). Result is cached until `refresh=True`.

**Cursor headless worktrees:** For a verified harness worktree under `.harness/worktrees/` (registered Git worktree), implementation calls add `--trust` and `--force` when `cursor.trust_harness_worktrees` is true (default). Review and plan repair do not get `--force`.

### Headroom routing

Implementation and review calls are routed by `choose_provider` (`harness/routing.py`) in **every** project, from the age-aware, machine-wide headroom snapshot (the same one shown on the live board). With no headroom data the result is exactly `providers.priority` order. The policy excludes disabled providers and providers disabled for the task, then applies these rules:

1. Start with `providers.priority`, followed by enabled providers omitted from it. The routing scenarios use the base order agy, Codex, Cursor, Claude; `choose_provider` itself works on whatever provider names are in `priority`/`enabled`.
2. **Cursor Auto first:** on a task's first implementation attempt only, if `cursor.auto_when_exhausted` is true and both known Cursor pools are at 100% or more, choose Cursor Auto before every other provider. Auto is never used for review.
3. Exclude a provider that has a usage window at 100%, and apply the Claude reserve below.
4. **Waste and urgency:** the quota that would expire unused is `remaining percent − hours to reset × full pace`. Providers with positive waste are *urgent* and go first, greatest waste first; ties keep configured order. Full pace comes from the S9-T31 pace source (`full_pace`), with routing fallbacks of 30%/hour for five-hour windows, 3%/hour for weekly windows and 1%/hour for monthly windows.
5. If no provider is urgent, use configured order.
6. **Review:** the implementing provider is removed when another provider has usable quota, so the reviewer is never the implementing provider when another reviewer exists, and Cursor Auto is never a reviewer. If only the implementer remains usable it may review.

The chosen provider and the routing reason are logged (`<task>-routing.log`) and stored for the board in meta `{sprint}.{task_id}.board.routing_reason`. If no provider is usable, the reason lists which are disabled, exhausted, over the Claude cap or unavailable.

**Claude reserve.** Claude is capped so it stays available when other providers are still out:

- Five-hour used-percent cap: `claude.reserve.min_percent` (60) when another provider returns within `min_hours` (1) hour, rising linearly with the hours until another provider returns, and reaching `max_percent` (90) at `max_hours` (5) or later. A three-hour wait therefore gives a 75% cap.
- Weekly used-percent cap: 100 minus `weekly_percent_per_day` (7) per day until Claude's weekly reset. An unknown reset uses the strictest value, as though seven days remained.
- Claude usage at or above either cap is not routed to Claude.

**Five routing scenarios** (order agy, Codex, Cursor, Claude, with the full-pace values above; executable in `tests/harness/test_routing_scenarios.py`):

1. With ordinary headroom and no urgency, agy implements; Codex reviews because agy implemented.
2. When Codex has 80% left in a five-hour window that resets in one hour, 50% would be wasted, so Codex implements; agy reviews.
3. When agy and Codex are both urgent, the larger waste wins (agy 40%, Codex 20%); Codex then reviews.
4. When both Cursor pools are at 100%, Cursor Auto gets the first implementation attempt. It never reviews; agy reviews. If Auto quota-disables Cursor for the task, the next attempt routes to agy.
5. When only Claude has usage, it runs only under the reserve: with another provider returning in three hours, 50% used is under the 75% cap but 76% waits; if nothing returns for six hours the cap is 90% and 76% may run. When Claude is the only usable reviewer it may review its own implementation.

**Cursor Auto when exhausted.** `cursor.auto_when_exhausted` (default `false`): Cursor uses its configured named models while either pool is below 100%. Only while both pools are known and at 100% does `auto` replace the named model on the task's first implementation attempt (the Auto-first attempt applies only then). Later attempts use normal headroom routing and named Cursor models, and Auto never replaces a review model. With missing headroom, normal provider order and named Cursor models apply.

### Per-task and per-phase routing

`ProviderAgentRunner` owns provider choice and attempt indices in `runtime.provider_attempts` (`agy`, `codex`, `cursor`, `claude`).

**Task-wide Codex disable** (metadata `{sprint}.{task_id}.provider.codex.disabled`): set when Codex hits quota, auth, model-unavailable, CLI/auth probe failure, or integration error **and** Cursor fallback is enabled. The **same** agent call then continues on Cursor without entering a waiting status. Value is a reason string (for example `quota:5h`, `auth`, `auth_or_cli_unavailable`). Claude quota or auth failures similarly set `{sprint}.{task_id}.provider.claude.disabled` so routing selects another provider; launch failures do not spend an implementation attempt.

**Task-wide Cursor disable** (`{sprint}.{task_id}.provider.cursor.disabled`): set the same way when Cursor returns quota, auth, model-unavailable or a launch failure and another provider is usable (for example after Cursor Auto hits quota). Cleared with the Codex disable on a **new** sprint run manifest, not on `resume`. Agy and Claude disable keys are not cleared by a new manifest; like the others, quota-reason disables of any provider are cleared when the task leaves `WAITING_QUOTA` or live headroom shows usable capacity again.

**Per-phase provider disable** (`{sprint}.{task_id}.provider.{provider}.disabled.{phase}`): after **three** stall/timeout events for the same phase and provider, `fallback_after_stalls` may disable that provider for that phase only and record a switch to the other provider (to whichever provider headroom routing then picks, with the phase-local disable applied; a Codex pick requires a fresh availability probe to succeed). Unlike Codex quota/auth fallback, this is **not** a same-call continue: `_handle_agent_wait` still pauses with `WAITING_TIMEOUT`, `waiting_provider` stays the stalled provider, and the alternate provider runs only on the next `harness run` / `harness resume` (with `capacity.policy: wait`, after `retry_interval_minutes`).

Each **new** run manifest clears task-wide Codex/Cursor disable keys so every task probes Codex again. **Resume** preserves prior fallback. Headroom routing also clears temporary quota disables when a task leaves `WAITING_QUOTA` or the provider has usable live capacity; for agy, either weekly pool below 100% counts as capacity at this reconciliation step.

### Codex vs Cursor on failure (same call)

| Failure class | Codex behavior when `cursor.enabled` | Cursor behavior |
| --- | --- | --- |
| Quota (output matches Codex quota patterns) | Disable Codex for task; **continue on Cursor in the same call** — no `WAITING_QUOTA` | Enters quota wait path (see below) |
| Auth / model unavailable | Disable Codex for task; continue on Cursor | Treated as a normal failed call (retry loop), not automatic provider switch |
| Capacity / network / provider error / timeout | Return result; scheduler may **wait**; Codex **not** disabled | Return result; scheduler may **wait** |
| Codex unavailable with Cursor disabled | Return failure (`quota_exhausted`, `auth_failure`, etc.) | N/A |

If Codex quota/auth/model failure occurs and Cursor is **disabled**, the harness does **not** fall back; quota exhaustion can still lead to `WAITING_QUOTA` when the returned result has `quota_exhausted`.

The implement loop generalizes this to every provider: when agy, Codex, Cursor or Claude returns quota, auth, model-unavailable or a launch failure and another candidate is still usable, the failing provider is disabled for the task (reason such as `quota:gemini-weekly`) and the same call continues on the next routed provider. With no other candidate the result is returned and the normal wait/retry handling applies. Claude failures are classified into quota, authentication, network, provider-busy, provider-error and timeout/stall results (quota phrases accept straight and typographic apostrophes); timeout or stall takes precedence and becomes `WAITING_TIMEOUT`, busy becomes capacity exhaustion, network and provider errors are transient waits, and a nonzero exit or reported stream error without a classified wait is an ordinary Claude failure.

**Agy result classification.** Limit and error phrases are considered only when the call failed (nonzero exit, timeout/stall, non-`SUCCESS` result), and never merely because they appear in a successful answer. Credits/quota/`RESOURCE_EXHAUSTED` phrases are quota, scoped to the selected pool and treated as a cap until its reset. `model capacity exhausted` and `rate limit` are temporary provider-busy capacity. Authentication phrases set auth failure; network and HTTP 5xx-style phrases set their corresponding transient wait causes. Quota beats timeout, while timeout/stall beats capacity/network/provider error. An unclassified failed agy call is an ordinary failure rather than a provider-error wait.

### Implementation attempt budget

With `ProviderAgentRunner`, the implement loop continues while `runtime.attempt < max_attempts` **and** `has_implementation_budget` is true.

**Outer cap** (`Scheduler._defaults` → loop `max_attempts`):

| Condition | Cap |
| --- | --- |
| CLI `--model-class` | **1** |
| Task sets `max_attempts` | that integer (not 1) |
| Otherwise | `max_implementation_attempts()` = `len(codex.implementation_sequence) + max(1, len(retry.sequence))`, plus the agy and Claude sequence lengths when those providers are enabled |

`max_implementation_attempts()` always returns that sum. It does not read `task.max_attempts` or `--model-class`.

**Live implement-call cap** (`has_implementation_budget`):

- `--model-class`: one Cursor call (`provider_attempts["cursor"] < 1`). Not `len(retry.sequence)`.
- Otherwise headroom routing decides. A provider whose own budget is used up is treated as unavailable for this check: agy, Codex and Claude when their provider-attempt count reaches their implementation sequence length, and Cursor when it reaches `max(1, len(retry.sequence))`. The task has budget while `choose_provider` still returns a provider from the rest. Ordinary failures consume one attempt of the provider that ran; the next attempt is routed again, so exhausting one provider's sequence falls through to another usable provider and the task is blocked only when none remains.
- Cursor model class uses effective sequence `retry.sequence` if non-empty, otherwise `[task.model_class]`; index `i` (from `provider_attempts["cursor"]`) picks `sequence[min(i, len(sequence) - 1)]`. With default empty `retry.sequence`, that is one attempt at the task’s `model_class`. With `cursor.auto_when_exhausted` and both pools at 100%, the first attempt uses `auto` instead.

Implementation and review always go through headroom routing, in every project; plan repair does so only when `claude.enabled`. The legacy `execution.default_max_attempts` path (`len(retry.sequence)` if non-empty, otherwise `2`) remains only for runners that do not manage model routing.

**Manual `--model-class`** (on `harness run-task`, `harness rerun-blocker`, etc.): forces **Cursor** only, single attempt, no automatic provider sequence for that invocation.

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

Runtime fields: `waiting_phase` (`implement` | `review`), `waiting_provider` (`agy` | `codex` | `cursor` | `claude`). Sprint-level meta `{sprint_id}.run_status` mirrors the pause for manifests.

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

**Important:** `WAITING_QUOTA` is recorded only when the **agent result** has `quota_exhausted` — for example agy or Cursor quota when no alternate provider remains, or Codex quota when fallback is disabled. When another provider is usable, the implement loop disables the exhausted provider and continues in the same call instead of waiting.

### Failure records and exit codes

| Kind | When | Suggested command |
| --- | --- | --- |
| `QUOTA_WAIT` | Stage stopped for quota | `harness resume <sprint>` |
| `CAPACITY_WAIT` | Stage stopped for capacity/transient/timeout wait | `harness resume <sprint>` |

`harness run` / `harness resume` with `quota.policy` / `capacity.policy` **`stop`** exits without sleeping (quota → exit code **4**, capacity → **5**).

### Policies, sleep, and probes

| Config | Default | Quota | Capacity |
| --- | --- | --- | --- |
| `quota.policy` / `capacity.policy` | `"wait"` | `wait` \| `stop` | `wait` \| `stop` |
| Sleep duration | — | `quota.probe_interval_minutes` (**60**) or until `quota.reset_at` + `quota.reset_grace_minutes` (**5**) | `capacity.retry_interval_minutes` (**5**) |
| `quota.keep_awake` / `capacity.keep_awake` | `true` | Windows keep-awake during quota sleep | Same during capacity sleep |
| `quota.teardown_environment_while_waiting` / `capacity.teardown_environment_while_waiting` | `true` | Tear down per-task Docker when pausing | Same |

**Reset time:** `quota.reset_at` in `harness.yaml`, or runtime override in state meta `quota.reset_at_override` (set/cleared via CLI). `QuotaManager.configured_reset_at()` prefers the override, then yaml, else unknown. When the configured reset is unknown or already past, the quota sleep ends at the earliest reset of the live exhausted windows (latest full-window reset per provider) plus a short grace, and otherwise falls back to `quota.probe_interval_minutes`.

**Automatic resume:** Only inside an active `harness run` or `harness resume` loop when `quota.policy` / `capacity.policy` is `wait`: after sleep, the loop calls `run_sprint` again. Ctrl+C is safe; manifest may show `PAUSED_BY_USER`. There is no background daemon. Focused `harness run-task` (including via `harness rerun-blocker` on a blocked task) records `QUOTA_WAIT` / `CAPACITY_WAIT` and exits **4** / **5** without sleeping, regardless of policy.

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

**Capacity (Cursor):** `capacity.busy_patterns`, `capacity.network_patterns`, `capacity.provider_error_patterns`.

**Capacity (Codex):** `codex.busy_patterns`, `codex.network_patterns`, `codex.provider_error_patterns`.

**Agy:** fixed runner patterns distinguish credits/quota/`RESOURCE_EXHAUSTED`, temporary model capacity/rate limits, authentication, network, and HTTP 5xx-style provider errors as described above; they are not configurable YAML keys.

**Legacy yaml pattern keys (`codex:` and `capacity:` sections):** on load, `HarnessConfig._load_wait_patterns` merges legacy `capacity_patterns` and `exhaustion_patterns` into `busy_patterns`, and legacy `transient_patterns` into `provider_error_patterns` (unknown legacy transient strings are treated as provider-error-shaped). The `quota:` section uses `quota.exhaustion_patterns` only (no merge helper).

Codex also uses `codex.auth_patterns` and `codex.model_unavailable_patterns` for fallback (not waiting statuses).

## Known gaps

- Persisted status `WAITING_FOR_CAPACITY` maps to `WAITING_PROVIDER_ERROR`, not to `WAITING_PROVIDER_BUSY`, even when the original failure was overload-shaped.
- Cursor pattern classification runs before the timeout override; a timed-out or stalled call is always treated as `WAITING_TIMEOUT` regardless of network-like output in the log.
- Codex only sets `waiting_cause: timeout` when the hard timeout or stall watchdog fired. Output that merely matches `timeout` / `timed out` in `codex.provider_error_patterns` is classified as `provider_error` → `WAITING_PROVIDER_ERROR`, not `WAITING_TIMEOUT`.
- Codex `_classify` checks auth and model-unavailable patterns before the timeout branch, so auth-shaped text in diagnostics can trigger same-call Cursor fallback (when enabled) instead of a capacity wait even when the call also timed out.
- Plan repair (`PlanChangeManager.propose` via `harness plan-change propose`) uses the legacy Codex/Cursor selection unless `claude.enabled` activates headroom routing, where agy can also be selected when enabled. `quota_exhausted`, `capacity_exhausted`, and other non-ok results raise `RuntimeError`; they do not set `WAITING_*`, sleep, or resume. The wait statuses in this document apply to implement and review only.
