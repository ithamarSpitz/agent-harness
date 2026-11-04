# Configuration

Project settings live in `harness.yaml` at the repository root. Values below are defaults from `HarnessConfig.load` in `harness/config.py` when a key is omitted.

Legacy keys are ignored or merged for compatibility: `execution.require_clean_base`; `code_map.enabled` and `code_map.default_for_tasks`; codex/capacity `capacity_patterns`, `transient_patterns`, and `exhaustion_patterns` (merged into the modern pattern lists).

## Configuration reference

### `execution`

| Key | Default | Meaning |
| --- | --- | --- |
| `max_parallel_agents` | `3` | Maximum concurrent tasks per stage. |
| `default_max_attempts` | `2` | Used in `_defaults` only when the runner does not set `manages_model_routing` and `retry.sequence` is empty. Production installs `ProviderAgentRunner`, so this is not the live attempt cap; see **Known gaps**. |
| `default_timeout_minutes` | `35` | Agent call hard timeout when a task omits `timeout_minutes`. |
| `base_ref` | `null` | When set, the first run of a sprint requires the integration checkout HEAD to match this ref. Resumes continue from the recorded integration branch. |
| `keep_successful_worktrees` | `false` | Keep worktree directories after a task reaches `DONE`. |
| `stall_timeout_minutes` | `12.0` | Watchdog, independent of the hard timeout. **Cursor** kills the agent only when both CLI output and worktree file activity have been silent this long (`last_activity = max(output_activity, last_repo_activity)`). **Codex** watches CLI output only; file activity does not reset the timer. |
| `watchdog_poll_seconds` | `10.0` | Poll interval for the agent process watchdog and queue pickup. |
| `keep_awake_during_run` | `true` | On Windows, prevent sleep for the full live run/resume lifecycle. |

### `worktree`

| Key | Default | Meaning |
| --- | --- | --- |
| `setup_commands` | `[]` | Shell commands run after worktree creation. |
| `setup_timeout_minutes` | `10` | Per-command timeout for setup commands. |

### `code_map`

| Key | Default | Meaning |
| --- | --- | --- |
| `command` | `"graphify"` | Graphify CLI executable used for code-map build/update. |
| `build_timeout_minutes` | `5` | Timeout for code-map build commands (for example `graphify update`). |

### `cursor`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | Allow Cursor agent calls when selected as the active provider. |
| `command` | `"agent"` | Cursor CLI executable. |
| `output_format` | `"stream-json"` | CLI output format (enables usage parsing). |
| `trust_harness_worktrees` | `true` | Pass `--trust` only for Git worktrees under `.harness/worktrees` that the harness can verify as registered worktrees. |
| `models` | `{}` | Map from model class name to Cursor model id. Every class referenced by tasks or config must appear here or validation fails (plan-repair planner class missing is a warning only; see `plan_repair`). |
| `auto_when_exhausted` | `false` | On a task's first implementation attempt, use Cursor `auto` instead of named models only while both known Cursor pools are at 100%. Later attempts use named models; Auto never reviews. |

### `codex`

Codex uses its own CLI flags (`sandbox`, `approval_policy`, `network_access`, per-attempt profiles). Cursor implementation does not use these keys.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Allow Codex CLI when it wins `providers.priority` and passes availability checks. |
| `command` | `"codex"` | Codex executable. |
| `reasoning_effort` | `"high"` | Default reasoning effort when an `implementation_sequence` entry is a plain string. |
| `sandbox` | `"workspace-write"` | Default Codex sandbox mode for implementation. |
| `windows_implementation_sandbox` | `null` | On Windows, `_effective_sandbox` uses this instead of `codex.sandbox` when the Codex call is not read-only. Implementation is always writable. Plan-repair is always read-only. Review is `read_only=not ui_review.enabled`, so when `ui_review.enabled` is true, review is writable and this override applies on Windows. |
| `approval_policy` | `"never"` | Codex approval policy. |
| `network_access` | `true` | Codex network access. |
| `implementation_sequence` | four profiles: `gpt-5.6-sol` with `medium`, `medium`, `high`, then `gpt-6-astra` with `medium` | Per-Codex-attempt model and `reasoning_effort` (string entries inherit `reasoning_effort`). |
| `review_model` | `"gpt-5.6-sol"` | Codex model for review when Codex is the review provider. |
| `planner_model` | `"gpt-5.6-sol"` | Codex model for plan-repair when Codex is the plan-repair provider. |
| `auth_required_substring` | `"Logged in using ChatGPT"` | Substring that indicates a successful auth probe. |
| `short_quota_patterns`, `weekly_quota_patterns`, `quota_patterns`, `busy_patterns`, `network_patterns`, `provider_error_patterns`, `auth_patterns`, `model_unavailable_patterns` | (defaults in `harness/config.py`) | Substrings used to classify Codex CLI output for waits, fallback, and routing. |

### `claude`

Claude Code uses `stream-json` and passes the configured model, effort, and permission mode to the CLI.

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Allow Claude Code to participate in provider routing. |
| `command` | `"claude"` | Claude Code executable. |
| `implementation_sequence` | `sonnet/medium`, `sonnet/high`, `opus/high` | Successive implementation profiles. Each item has a non-empty `model` and an `effort` of low, medium, high, or max. The list must not be empty. |
| `review_model` | `"sonnet"` | Model for review and plan repair. |
| `review_effort` | `"high"` | Effort for review and plan repair. |
| `escalation_model` | `"opus"` | Model selected by an explicit escalation model class. |
| `escalation_effort` | `"high"` | Effort selected with `escalation_model`. |
| `permission_mode` | `"bypassPermissions"` | Value passed to Claude Code `--permission-mode`. |
| `reserve.min_percent` | `60` | Five-hour used-percent cap when another provider returns within `min_hours`. |
| `reserve.max_percent` | `90` | Five-hour used-percent cap from `max_hours` onward. |
| `reserve.min_hours` | `1` | Lower endpoint for the linear five-hour reserve. |
| `reserve.max_hours` | `5` | Upper endpoint for the linear five-hour reserve; must exceed `min_hours`. |
| `reserve.weekly_percent_per_day` | `7` | Weekly reserve per day remaining: cap is 100 minus this value times days to reset. |

Reserve percentages must be between 0 and 100, hour/day values must be nonnegative, and `max_percent` must be at least `min_percent`. Legacy `reserve.five_hour_max_used_percent` and `reserve.weekly_max_used_percent` are ignored with a warning.

### `providers`

| Key | Default | Meaning |
| --- | --- | --- |
| `priority` | `["codex", "cursor"]` | Base order for headroom routing. Enabled providers omitted from the list are appended. Add `"claude"` to place Claude explicitly. Urgent expiring quota may move a provider ahead of this order. |

### `retry`

Applies to **Cursor** implementation routing when Codex is not the active implement provider.

| Key | Default | Meaning |
| --- | --- | --- |
| `sequence` | `[]` | Cursor model class per attempt index. When empty, each Cursor attempt uses the task’s `model_class`. |

### `verification`

| Key | Default | Meaning |
| --- | --- | --- |
| `global_task_commands` | `["git diff --check"]` | Shell commands run in the task worktree after every task. |
| `stage_commands` | `[]` | Shell commands run on the integration checkout at the stage barrier. |
| `env` | `{}` | Default environment for verification subprocesses only (`setdefault`; never overrides values from the caller or task environment). |

### `paths`

| Key | Default | Meaning |
| --- | --- | --- |
| `protected` | `[]` | Repository paths agents must not change unless the task sets `allow_protected: true`. |
| `review_required` | `[]` | Glob patterns: if any changed file matches, independent review runs even when the task has `review: false`. Cannot disable review when `review: true`. |

### `context`

| Key | Default | Meaning |
| --- | --- | --- |
| `aliases` | `{}` | Logical names mapped to repo-relative paths for task `context` entries. |

### `environment`

Docker support is opt-in per task via `environment: docker` in the brief when allowed here.

| Key | Default | Meaning |
| --- | --- | --- |
| `docker_enabled` | `false` | Allow tasks to declare `environment: docker`. |
| `compose_command` | `"docker compose"` | Compose executable. |
| `compose_files` | `["docker-compose.yml"]` | Compose file paths. |
| `project_name_prefix` | `"harness"` | Prefix for Compose project names. |
| `services` | `[]` | Compose services the harness manages. |
| `validate_compose` | `true` | Run compose config validation before `up`. |
| `up_before_agent` | `false` | Start compose before the agent runs. |
| `teardown_on_success` | `true` | Tear down compose after task success. |
| `teardown_on_blocked` | `true` | Tear down compose when the task is blocked or waiting (when a task environment was started). |
| `remove_volumes` | `true` | Remove volumes on teardown. |
| `command_timeout_minutes` | `10` | Timeout for Docker-related shell commands. |
| `env` | `{}` | Merged into `task_env` and `stage_env` (`dict.update`, overrides ambient env). The scheduler passes those maps into implement, review, verification, compose, and related subprocesses. Unlike `verification.env`, which only fills missing keys (`setdefault`). |

### `quota`

Primarily drives **Cursor** quota waits (pattern lists differ from Codex’s codex-section lists).

| Key | Default | Meaning |
| --- | --- | --- |
| `policy` | `"wait"` | `wait` or `stop` when quota exhaustion is detected. |
| `reset_at` | `null` | Optional ISO timestamp for quota reset waiting. |
| `probe_interval_minutes` | `60.0` | Sleep interval when no `reset_at` is configured. |
| `reset_grace_minutes` | `5.0` | Extra wait after `reset_at`. |
| `keep_awake` | `true` | On Windows, prevent sleep during quota waits. |
| `teardown_environment_while_waiting` | `true` | Tear down the task Docker environment while waiting. |
| `exhaustion_patterns` | (defaults in `harness/config.py`) | Substrings for Cursor quota exhaustion. |

### `capacity`

| Key | Default | Meaning |
| --- | --- | --- |
| `policy` | `"wait"` | `wait` or `stop` on capacity or classified transient provider errors. |
| `retry_interval_minutes` | `5.0` | Sleep between capacity retries. |
| `keep_awake` | `true` | On Windows, prevent sleep during capacity waits. |
| `teardown_environment_while_waiting` | `true` | Tear down the task Docker environment while waiting. |
| `busy_patterns`, `network_patterns`, `provider_error_patterns` | (defaults in `harness/config.py`) | Substrings for capacity/transient classification (separate from Codex’s codex-section lists). |

### `plan_repair`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` | When `false`, `harness plan-change propose` exits with “Plan repair is disabled in harness.yaml”. `harness validate` still runs; when `enabled` is `true` and `planner_model_class` is not declared under `cursor.models`, validate emits a **warning** only (ordinary task execution still runs). |
| `planner_model_class` | `"escalation"` | Default Cursor model class for `plan-change propose` (must exist in `cursor.models` for propose to succeed). |
| `timeout_minutes` | `20` | Timeout for the plan-repair agent call. |

Plan-repair uses Codex when Codex wins `providers.priority` for phase `plan_repair` and is enabled; otherwise Cursor with `planner_model_class` (or `--model-class`).

### `ui_review`

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `false` | Enable UI review instructions and related cleanup. |
| `start_commands` | `[]` | Commands the reviewer may use to start the app (required when `enabled` is true). |
| `url` | `null` | URL for browser tools (required when `enabled` is true). |
| `ports` | `[]` | Ports to monitor or clean up. |
| `playwright_mcp_package` | `"@playwright/mcp@0.0.84"` | Documented Playwright MCP package name for UI review tooling. |

### `release`

| Key | Default | Meaning |
| --- | --- | --- |
| `commands` | `[]` | Shell commands run after a sprint completes successfully. |
| `timeout_minutes` | `15` | Timeout for **each** release command (`subprocess.run` per command); multiple commands may each use the full limit. |

### `doctor`

`Doctor.run` returns before this section unless `full=True` (`harness doctor --full`). Plain `harness doctor` ignores these keys.

| Key | Default | Meaning |
| --- | --- | --- |
| `required_commands` | `[]` | Commands that must exist on `PATH` (`--full` only). |
| `required_env` | `[]` | Environment variables that must be set (`--full` only). |
| `min_free_disk_gb` | `1.0` | Minimum free disk space required (`--full` only). |
| `check_cursor_models` | `true` | Validate access to configured Cursor models (`--full` only). |
| `minimum_versions` | `{}` | Tool name to minimum version string (`--full` only). |

## Task frontmatter

Task briefs use YAML frontmatter between `---` delimiters. `task_spec_from_brief` in `harness/roadmap.py` reads these keys (roadmap task rows merge on top of the brief for listed tasks).

| Key | Default | Meaning |
| --- | --- | --- |
| `id` | (required in practice) | Task id; must match the roadmap entry and the brief’s logical id. |
| `stage` | `1` | Stage number; a roadmap task row can override the brief value. |
| `depends_on` | `[]` | Task ids that must be `DONE` before this task starts. |
| `model_class` | `"worker"` | Cursor model class for implementation when Cursor implements and `retry.sequence` is empty. |
| `review_model_class` | `null` | Cursor model class for review when Cursor reviews; when unset, Cursor uses `"reviewer"`. Codex and Claude use their provider-specific review profiles instead. |
| `max_attempts` | `null` | Caps the attempt budget from `_defaults`. When unset, provider routing sums the Codex sequence, Cursor budget, and (when enabled) Claude sequence. Each live call also requires remaining budget for a currently usable provider. |
| `timeout_minutes` | `null` | Agent timeout; otherwise `execution.default_timeout_minutes`. |
| `review` | `true` | Primary review switch. Review runs when `review` is true **or** any changed file matches `paths.review_required` (`review_required = task.review or verifier.requires_review(...)`). `paths.review_required` can force review on but cannot turn review off when `review` is true. |
| `allow_protected` | `false` | Allow edits under `paths.protected`. |
| `allowed_paths` | `[]` | Glob patterns for permitted edits. Loader default is empty; `harness validate` and `harness add-task` / the queue reject an empty list. Verification skips the scope check when the list is empty (`if task.allowed_paths:`), so `[]` means unrestricted diffs, not “no files allowed”. |
| `verification` | `[]` | Extra shell commands run in the task worktree after global task verification commands. `harness validate` requires at least one task verification command. |
| `context` | `[]` | Context paths or `context.aliases` keys injected into agent prompts. |
| `environment` | `null` | Task environment mode (for example `docker` when `environment.docker_enabled` is true). |

### Roadmap (`roadmap.yaml`) sprint entries

Not task frontmatter, but loaded with tasks:

| Key | Meaning |
| --- | --- |
| `name` | Sprint display name (defaults to sprint id). |
| `tasks` | Map of task id → row with at least `file`; row fields merge over brief frontmatter (`stage`, `depends_on`, model fields, etc.). |
| `stage_verification` | Map of stage number → list of shell commands for the stage barrier. |

## Known gaps

- **`TaskStatus.READY`** is defined but no live transition assigns it; scheduling uses `PENDING` and direct moves into `IMPLEMENTING` or waiting states.
- **Detached HEAD** on the integration checkout is rejected (`current_branch` requires a named branch).
- **Codex `implementation_sequence`** defaults to four entries in code; starter `harness init` YAML may omit them and rely on loader defaults.
- **`execution.default_max_attempts`** is loaded (default `2`) but production always uses `ProviderAgentRunner` (`manages_model_routing`), which skips that branch in `_defaults`. With default `codex.enabled: false` and empty `retry.sequence`, Cursor implementation budget is typically one call per task unless Codex is active or `retry.sequence` adds attempts.
- **`harness init` template comment** suggests `max_attempts: null` uses `execution.default_max_attempts`; live routing follows `_defaults` / `ProviderAgentRunner` behavior above, not that comment alone.
