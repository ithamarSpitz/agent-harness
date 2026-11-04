# Review, UI check, and code map

Independent review runs after mechanical verification succeeds (or is skipped on a matching review resume). Optional Graphify code maps and optional browser UI checks extend what the reviewer sees. This document describes current harness behavior.

## Review

### When review runs

Review runs when either condition holds after verification succeeds (or is skipped on resume; see below):

| Source | Rule |
| --- | --- |
| Task brief `review` | Default `true`. When `false`, review is skipped unless `paths.review_required` matches. |
| `paths.review_required` | Glob patterns (`fnmatch` against changed paths). Any match forces review even if `review: false`. Cannot turn review off. |

Computation: `review_required = task.review or verifier.requires_review(changed_files)`.

### What the reviewer receives

The harness builds a text prompt (`review_prompt`) and sends it to the selected provider. The reviewer is told not to modify files. The prompt includes:

| Section | Content |
| --- | --- |
| Instructions | Task id; read `AGENTS.md`, the task brief path, and harness-resolved context paths (from the task `context` config). |
| Diff | Instruction to review the working tree diff against worktree `base_ref` (the agent runs Git in the worktree). |
| Verification | Full mechanical verification summary from the just-passed run (or the fixed resume message when verification was skipped). |
| Code map | Optional paragraph when a map was copied into the worktree (see Code map). |
| Outside-diff users | A generated **Code outside the diff that uses what changed:** list for changed Python functions, classes, and methods. |
| Impact instructions | A required pre-verdict impact check and a required post-verdict `IMPACT:` section. |
| UI check | Optional instructions when `ui_review.enabled` (see Reviewer UI check). |

Before deciding its verdict, the reviewer must list every changed function, class, config key, CLI command, or output format; find users outside the diff; and check that those users still behave correctly. After the verdict it must write an `IMPACT:` section listing each changed item and the users checked, or `none outside the diff`. The scheduler's pass decision still uses the exact `VERDICT: PASS` line.

The harness precomputes the outside-diff list. It identifies changed Python symbols from diff ranges (and definitions in untracked Python files), then reads Graphify relations `calls`, `imports_from`, `uses`, and `references`, excluding every changed file. Each entry is `path:line -> qualified symbol`, capped at five users per symbol and 40 lines overall. If the graph is absent or unreadable, it runs word-boundary `git grep` over Python files, excludes changed files, and labels the prompt `no code map, used grep` or `code map unreadable, used grep`. Collection never blocks review; no matches becomes `none found`.

Agent logs use provider-specific names under the harness log directory, including `{task-id}-review.log` for Cursor, `{task-id}-review-codex.log` for Codex, and `{task-id}-review-claude.log` for Claude.

### Provider choice

`ProviderAgentRunner.review` uses headroom routing:

| Provider | Review model |
| --- | --- |
| Codex | `codex.review_model` (default `"gpt-5.6-sol"`). |
| Cursor | `cursor.models[review_model_class or "reviewer"]`. |
| Claude | `claude.review_model` with `claude.review_effort` (defaults `sonnet/high`). |

When more than one provider is usable, routing removes the provider that implemented the task, so the reviewer is never the implementer once another reviewer exists. When it is the only provider with usage, the implementer may review. Cursor Auto is implementation-only and never reviews. Provider quota/auth failures can disable that provider for the task and reroute; transient capacity waits do not.

### Verdict parsing

`_verify_review_commit` runs `_handle_agent_wait(..., phase="review")` **before** the `VERDICT: PASS` check. Quota, capacity/transient, and timeout/stall therefore never reach verdict parsing.

| Outcome | Condition |
| --- | --- |
| Wait | `quota_exhausted`, `waiting_cause`, timeout (`timed_out`), or stall (`stalled`). Status becomes the matching `WAITING_*` with `waiting_phase == "review"`. Timeout/stall uses `waiting_cause="timeout"` → `WAITING_TIMEOUT`. After three stalls for the same review provider: provider fallback, or `BLOCKED` (`"stalled 3 times"`). Not `RETRY`. |
| Pass | Wait handling returned no pause, agent result `ok` is true, **and** at least one output line equals exactly `VERDICT: PASS` after stripping surrounding whitespace. |
| Fail | Wait handling returned no pause, and Pass does not hold: missing line, `VERDICT: FAIL`, agent error, or synthetic fail (UI worktree edits). |

The prompt requires the **first non-empty line** to be exactly `VERDICT: PASS` or `VERDICT: FAIL`. The scheduler does not enforce “first line” on pass; it accepts `VERDICT: PASS` on any line.

On Fail, status becomes `RETRY`. Full review output (or agent error text) is stored in both `review_feedback` and `last_error`.

On pass, the harness commits all changes in the worktree with message `harness: {task-id}`, sets status `PASSED`, and clears `review_feedback` and `last_error`.

When review is not required, verification success goes straight to commit with no reviewer call.

### What travels to the next implementation attempt

The implement prompt includes a retry block when `previous_failure` is set. After a failed review or verification, the scheduler sets `previous_failure` from `runtime.last_error or runtime.review_feedback`. That text is the full review output on review failure, so the next implement attempt sees the reviewer’s findings.

Review-phase waits neither increment nor decrement `runtime.attempt`; it stays where implementation left it. The wait call uses `decrement_attempt=False`, which means the already-incremented implement attempt is **not** given back (unlike implement-phase waits, which pass `decrement_attempt=True` and decrement).

### Worktree fingerprint after verification

When verification runs successfully, the harness records:

| State metadata key | Value |
| --- | --- |
| `tasks.{task_id}.verification_commit` | Worktree `HEAD` at verification time (written once if not already set). |
| `tasks.{task_id}.verification_fingerprint` | SHA-256 over porcelain status plus content of files changed vs `base_ref`. |

These support review resume without re-running verification when the tree is unchanged.

### Resume after a review-phase wait

If status is any of `WAITING_QUOTA`, `WAITING_NETWORK`, `WAITING_PROVIDER_BUSY`, `WAITING_PROVIDER_ERROR`, or `WAITING_TIMEOUT` **and** `waiting_phase == "review"`, the next `run`/`resume` enters `_verify_review_commit` with `resume_review=True` instead of starting a new implementation attempt.

| Step | Behavior |
| --- | --- |
| Skip verification? | Yes when `_review_resume_skips_verification` is true: recorded verification commit equals current `HEAD`, and current fingerprint equals `tasks.{task_id}.verification_fingerprint`. Applies to **every** review-phase wait (quota, capacity/transient, timeout/stall), not quota alone. |
| Verification skipped | Summary text: `Task verification passed before the reviewer wait.` Review still runs (`review_required` treated as true). |
| Verification not skipped | Status `CHECKING`; verification re-runs; on success, fingerprint is re-recorded and review runs if required. |
| Review waits again | Same review-phase wait handling; implementation attempt is not consumed. |

While pausing for quota or capacity during review, `waiting_phase` is set to `"review"`.

### UI review side effects (summary)

When `ui_review.enabled`, after the reviewer returns, the harness compares worktree snapshots; file changes fail review with synthetic `VERDICT: FAIL` (details under Reviewer UI check). Cursor may pass `--approve-mcps` when the CLI supports it; Codex injects a Playwright MCP server for review when UI review is enabled.

## Reviewer UI check

### Configuration

| Key | Default | Meaning |
| --- | --- | --- |
| `ui_review.enabled` | `false` | Append UI instructions to the review prompt; enable snapshot/cleanup and provider-specific Playwright setup. |
| `ui_review.start_commands` | `[]` | Multi-line block in the prompt: commands to start the app from the repo root. Required non-empty when enabled (`harness validate`). |
| `ui_review.url` | `null` | URL the reviewer should open with browser tools. Required when enabled. |
| `ui_review.ports` | `[]` | TCP ports used for listener discovery during cleanup. |
| `ui_review.playwright_mcp_package` | `"@playwright/mcp@0.0.84"` | npm package passed to Codex MCP config on review (`npx -y …`). |

### Prompt behavior

When enabled, the review prompt adds a **UI check (your decision)** section:

1. If UI behavior may change, start the app using `start_commands`, open `url` with Playwright browser tools, exercise the change, and **do not change any files**.
2. If UI cannot be affected, do not start anything.
3. After the `VERDICT` line, add exactly one of: `UI_CHECK: done - …` or `UI_CHECK: skipped - …`.
4. A UI problem is a FAIL finding.

The harness does not parse `UI_CHECK` lines for pass/fail; only the `VERDICT` line (and worktree snapshot) drive the outcome.

### Provider differences

| Topic | Cursor | Codex | Claude |
| --- | --- | --- | --- |
| Playwright MCP | Relies on reviewer MCP config; harness adds `--approve-mcps` when supported. | Injects only `mcp_servers.playwright` via `npx`. | Uses a strict temporary MCP config containing only the same Playwright server. |
| Permissions | Review stays unforced (no `--force`). | Read-only when UI review is off; with UI review it uses the configured writable sandbox so start commands can run. | Uses `claude.permission_mode`; the review edit snapshot still forbids retained file changes. |

### Worktree edit detection

Before review, the harness captures a worktree snapshot (`git status --porcelain` and hash of `git diff HEAD`). After review, it compares snapshots. Any status or diff change yields `fail_review_for_worktree_edits`: `ok=false`, output prefixed with `VERDICT: FAIL` and a list of changed paths (or a generic message if only diff hash changed).

### Process cleanup

Cleanup runs in a `finally` block after every UI-enabled review, success or failure.

| Target | Rule |
| --- | --- |
| Listeners on `ui_review.ports` | **Snapshot-based.** Before review, the harness records PIDs listening on configured ports. After review, it stops listeners on those ports whose PID was **not** in the pre-review snapshot. Pre-existing listeners are left running. |
| Worktree-related processes | **Always.** Any discovered process whose command line contains the task worktree path (with prefix-safe matching) is stopped, regardless of port snapshot. |
| Protected PIDs | Harness and related PIDs are never stopped. |

Stopped processes are logged to `{task-id}-ui-review-cleanup.log` in the harness log directory (one line per PID: `stopped pid=… command=…`). Discovery is platform-specific (PowerShell/CIM on Windows, `ps` and port queries on POSIX).

## Code map

Optional Graphify index shared across tasks. Config:

| Key | Default | Meaning |
| --- | --- | --- |
| `code_map.command` | `"graphify"` | CLI invoked as `{command} update . --no-cluster` and probed with `{command} --help`. |
| `code_map.build_timeout_minutes` | `5` | Timeout for the update subprocess. |

Legacy YAML keys `code_map.enabled` and `code_map.default_for_tasks` are ignored at load time.

### Build and cache (`ensure_project_map`)

Runs under `.harness/code-map/` (next to the harness runtime log root):

1. If `{command} --help` fails, log a warning and skip (no map).
2. If `commit.txt` records the requested `base_commit` and `source/graphify-out/graph.json` exists, skip rebuild.
3. Otherwise create or detach the `source` Git worktree at `base_commit`, run `{command} update . --no-cluster` in that worktree, then record the commit in `commit.txt`.

Build warnings go to `code-map.log` or `{task-id}-code-map.log`.

### Offer to task worktree (`offer_to_worktree`)

After a successful project map:

1. Ensure `graphify-out/` is listed in the repo’s `info/exclude` (git common dir).
2. Remove any existing `graphify-out/` in the task worktree, copy from `source/graphify-out/`, and write `graphify-out/AGENTS-graphify.md` (from the installed `graphify` package rules, or built-in fallback text).

Returns `true` only when Graphify is available and the project graph exists; otherwise implement/review prompts omit the code-map paragraph.

### Prompt text when offered

Implementation and review prompts append:

- A note that `graphify-out/` is available.
- Instruction to read `graphify-out/AGENTS-graphify.md` first if using the map.
- Use `--budget 500` on `{command} query` (first token of `code_map.command`).

### Usage accounting

Usage JSONL records `code_map: true/false` per agent call and `code_map_calls`: count of shell command lines in agent output that contain the configured command as a whole word (`count_code_map_calls`).

### Operator checks

`harness doctor` reports whether the configured Graphify command responds to `--help`. It does not build the map.

## Known gaps

| Area | Actual behavior |
| --- | --- |
| `harness review` CLI | Exit success when `result.ok` and substring `"VERDICT: PASS"` appears anywhere in output, not the scheduler’s exact-line rule. |
| Verdict line order | Prompt asks for `VERDICT` as the first non-empty line; the scheduler accepts `VERDICT: PASS` on any line. |
| `UI_CHECK` lines | Prompt requires them after UI review; the harness does not validate or store them separately. |
