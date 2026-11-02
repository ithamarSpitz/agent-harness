# Harness operator handbook

For whoever drives `harness` (usually Claude). Read before starting, resuming or repairing a run. Every rule here came from a mistake that cost real tokens or quota on the BookNook build (Oct 2026). `README-HARNESS.md` explains what the harness does; this file is how to operate it without wasting attempts.

## 1. Golden rules

1. **The integration checkout stays clean, and changes to it happen only while no `harness` process runs.** No untracked files ever. Commit (brief fixes, harness fixes) only when the harness is idle, for example after it stopped on a blocked task. Since the base-ref fix, existing worktrees are diffed against their own base, so such commits no longer show up as task changes. A task worktree keeps its old copy of `tasks/` and docs: to give a kept worktree a changed brief, fast-forward it (`git -C <worktree> merge --ff-only build`) and set that task's `base_ref` in `.harness/state.json` to the new head.
2. **Logs go outside the repo.** Redirect `run`/`resume` output to the scratchpad, never to `.harness/` or anywhere tracked. An untracked log made the first S0 run refuse to start.
3. **Never pay for the same diagnosis twice.** When a task fails, read the evidence (section 4) before any retry. A retry without a changed input repeats the failure.
4. **Operator mistakes look like agent failures.** A scope or protected-path violation on a file the task never touches (for example `harness.yaml`) is almost always caused by the operator, not the agent. Check what you changed first.

## 2. Before a run

- `harness validate`, `graph <SPRINT>`, `run <SPRINT> --dry-run`, `doctor --full`.
- `git status --short` must be empty.
- **Memory.** Check free RAM. Worktree setup (`npm ci`) runs once per parallel task at the same moment, on top of Docker and Supabase. With under about 6 GB free, use `max_parallel_agents: 2`. Claude Code kills background shells under memory pressure; the run then needs `resume`. It happened again at 2 agents with about 2 GB free (Docker/WSL 3 GB plus Chrome). After a kill: check for orphaned agent processes (`Get-CimInstance Win32_Process` filtered on `worktrees`), then `resume`; the task left `RUNNING` continues in its worktree. Ask the user to start Claude Code with `CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP=1` for long runs.
- **Quota.** Ask the user for, or check, Codex's 5-hour and weekly limits and Cursor usage. One blocked task (3 attempts plus reviews) can burn a 5-hour Codex window.
- **Cursor ladder stays native.** `retry.sequence` and `plan_repair.planner_model_class` use only Composer and Grok classes. Opus bills as "Other Models" or on-demand spend.
- **Stray processes.** Look for old dev servers (`supabase functions serve`, web servers) that would collide with verification ports, and stop them before a task that starts its own.
- **Tooling that verification relies on must work from a fresh clone.** If code imports packages (Deno `npm:` specifiers), the repo must resolve them itself (`deno.json` with `"nodeModulesDir": "auto"`). Turn off lock files that verification would rewrite (`"lock": false`), or the barrier finds a dirty checkout.

## 3. While it runs

- Start with the Bash tool's `run_in_background`; you are notified when the process exits. Do not poll.
- One Monitor as a fallback, re-armed every 30 minutes. It reads `harness status` and reports a new `WAITING_FOR_QUOTA` / `WAITING_FOR_CAPACITY` or 60 minutes without a `state.json` change. Do not grep `state.json` for `WAITING`: leftover fields gave false alarms.
- Codex prints quota errors with a typographic apostrophe ("You’ve hit your usage limit"). Patterns now normalize it; if a quota looks like a capacity wait again, check the raw `.harness/logs/<TASK>-attempt-N-codex.log`.
- Provider fallback is per task: each new task tries Codex first, a task that hits a quota stays on Cursor for its own retries and review.

- After an `Agent stalled` or timeout attempt, check that only one agent process works in that worktree (filter `Win32_Process` on the worktree path; compare start times). Killing processes may need the user's approval in auto mode.

## 4. Reading a failure (cheap first)

1. `harness status <SPRINT>` and `failure <SPRINT>`.
2. The last agent message and the reviewer verdict, from the JSON logs:

   ```python
   import json
   for line in open(".harness/logs/<TASK>-review-codex.log", encoding="utf-8", errors="replace"):
       o = json.loads(line); it = o.get("item") or {}
       if o.get("type") == "item.completed" and it.get("type") == "agent_message": last = it["text"]
   print(last)
   ```
3. `.harness/logs/<TASK>-verification.log` tail: the `Failures:` block names the exact reason.
4. Classify before acting:

| Failure | Typical cause | Action |
|---|---|---|
| `Changed files outside allowed_paths` / `Protected files changed` on a file the task does not own | operator changed the base, or a stray file | fix the cause, then rerun; do not blame the brief |
| Reviewer `VERDICT: FAIL` listing spec gaps | the brief or architecture left a decision open | decide it (section 5), do not just add attempts |
| Agent `STATUS: blocked` citing a conflict | real gap in docs | decide it, commit the docs, `reset-task` |
| Stage barrier failed, tasks `VERIFIED` | integration/tooling problem | fix, commit, `rerun-blocker <SPRINT>` (barrier only), then `resume` |
| Quota or capacity wait | provider limits | nothing; it resumes by itself |

## 5. Recovery commands that actually work

- **One blocked task:** `rerun-blocker <SPRINT>` (fresh budget, same worktree, previous feedback included), then `resume <SPRINT>`.
- **Several blocked tasks:** `run-task <TASK> --fresh-budget` for each, then `resume <SPRINT>`. `rerun-blocker` refuses when more than one task is blocked unless given `--task`.
- **`unblock` followed by `resume` does not work** while the sprint has a failure record: `resume` stops immediately. Use the two commands above.
- **Brief or architecture changed:** commit it, then `reset-task <TASK>`. A kept worktree still contains the old brief and docs, so the agent would never see the change.
- **Reviewer keeps finding new edge cases on a spec-type task** (contracts, docs): after two review rounds, fix the remaining points yourself in the task worktree, then `run-task <TASK> --fresh-budget` so the independent review records the pass. Each extra round costs about 10 minutes of Codex.
- `--model-class escalation` forces a one-shot Cursor call on the escalation model (Opus). Avoid unless the user agrees to the cost.

- **Stop review loops by deciding.** When a reviewer's findings swing between opposite demands across rounds (delete vs restore vs do not touch), the brief left a decision open. Decide it, write it into the brief's acceptance criteria, sync the worktree (rule 1), fix the code yourself, then `run-task --fresh-budget`. When findings are real product bugs, more rounds are worth it.
- **Run the failing verification command yourself first.** A live gate failure can be the stack, an external API, or the code. One manual run in the worktree (with `BOOKNOOK_ENV_FILE` set) costs seconds; an agent round costs about 10 minutes and often only reports the same blocker.
- **`harness verify <TASK>` on Windows:** set `PYTHONIOENCODING=utf-8` when redirecting output, or Hebrew text crashes the print with a `charmap` error.

## 6. Writing briefs and contracts

- Give every file exactly one owner per stage. Before a sprint, check each brief's scope against the contract: if a parallel task needs a helper in a file another task owns, either give it a local copy (and let the integration gate consolidate) or move it to the contract task.
- Spec-type tasks need an explicit quality bar in the brief: self-contained (restate rules instead of "see architecture"), field-to-column tables, typed signatures, edge-case rules. Back the checkable parts with a script (`scripts/check-contract.mjs`).
- Before a sprint, list every file each task must change to meet its acceptance criteria (CLI options live in `harness/cli.py`, config keys in `config.py`) and check it is in that task's `allowed_paths`. S6 (Nov 2026): a brief required new `init` options but `cli.py` belonged to another task in the same stage; the task burned 3 attempts and blocked the stage. Give the shared file to one task and put the other in the next stage.
- Briefs are the smallest pieces: one behavior and its tests per task. When a task blocks on a stream of new review findings, never cut requirements: keep the finished part as the task (list the open findings in a "Split into follow-up tasks" section) and give each open finding area its own task in the next stages, then `run-task --fresh-budget` and `resume` (S7, Nov 2026).
- Keep `README.md` out of feature tasks; one docs task at the end owns it, so feature tasks can run in parallel.
- After a contract sprint, add its files to `paths.protected` (commit that before the next sprint starts, not during it).
- Tasks that share one stateful resource (the single local Supabase stack) must not run in the same stage.

## 7. Shared stack and live APIs

- **A memory kill during `supabase db reset` deletes the database container and volume.** Symptom: `No such container: supabase_db_<project>` and the other containers restarting. Fix: `npx supabase stop` then `npx supabase start` (operator only; agents must not). Local data is rebuilt from migrations.
- **Live gates are flaky by nature.** Wikidata rate-limits repeated runs (HTTP 429), worse without a `User-Agent`. A gate that failed and passed on the next run with the same code is an external cause, not a code change.
- **Cached negative results poison later runs.** If the app caches "not found" and the gate leaves the catalog in place, one bad run makes every later run fail. Clear the affected cache rows before rerunning, and make the app cache "not found" only when every provider answered.
- **Network outages look like RUNNING.** Codex retries transient provider errors without spending attempts, so a task can sit in RUNNING for an hour. The Monitor also reports when the newest log stops growing for 30 minutes.

## 8. Harness bugs fixed in this copy

| Bug | Symptom | Fix |
|---|---|---|
| Quota patterns used ASCII `'` | Codex quota treated as a capacity wait, retried every 5 minutes | normalize typographic apostrophes before matching |
| Codex fallback was per sprint | one quota hit moved every later task to Cursor | per-task flag `<sprint>.<task>.provider.codex.disabled` |
| Resumed worktree diffed against the new stage base | later integration commits reported as protected-file edits by the task | `_run_task` keeps the worktree's own `base_ref` |
| Cursor stall/timeout killed only the cmd/PowerShell shim | the stalled agent kept running in the worktree next to the retry (two agents, one worktree) | shared `stop_process_tree` (`taskkill /T`), Cursor started in its own process group |
| Cursor probe demanded exactly `OK` | probe failed on `OK.` | accept a trailing period |

These fixes are in the standalone harness (`dev/agent-harness`, installed once as the `harness` command). `dev/nyc-property-resolver` still has an old copy without them.

## 9. Code map and reviewer UI check (v0.13)

- **Code map (Graphify).** `code_map.enabled: true` in `harness.yaml` makes it available; a task gets it only with `code_map: true` in its frontmatter (or `default_for_tasks: true`). Mark only tasks that must find their way through unfamiliar code; a small, well-scoped task does not need it. The map is cached in `.harness/code-map/<base sha>/` and rebuilt only when the base commit changes. Agents are told to use only `graphify explain` / `graphify path` (about 150 tokens); `graphify query` prints about 15k tokens. `graphify.exe` must be on PATH (copied to `~/.local/bin`). Usage records carry a `code_map` flag for comparing token use.
- **Reviewer UI check (Playwright).** `ui_review` gives the reviewer the start commands, URL and Playwright tools; the reviewer decides itself whether a change can affect the UI and ends with a `UI_CHECK:` line. The harness only fails a review that changed files and stops leftover processes (configured ports, command lines containing the worktree path). Cursor needs Playwright in `~/.cursor/mcp.json`. Do not run another server on the configured ports during a sprint.

## 10. Running the harness on its own repo

- Drive it from a frozen, non-editable copy: `python -m venv <scratchpad>/hvenv`, `<scratchpad>/hvenv/Scripts/pip install C:/Users/itas1/dev/agent-harness`, then run `<scratchpad>/hvenv/Scripts/harness.exe` in the repo. Stage merges then cannot change the code that is running. Tests in task worktrees import the worktree's own `harness/` (conftest puts the repo root first).
- A status Monitor must match `STATUS  TASK-ID` (status first), e.g. `grep -oE "[A-Z_]+ +S1-T[0-9]+"`. The reversed pattern stayed silent for 30 minutes.
- Briefs that close every decision (exact config keys, prompt text, failure behavior) kept review rounds to real bugs only: S1 (Oct/Nov 2026) needed 3+3+1 attempts, with no swinging demands.

## 11. Install and release (from v0.14)

- The harness is installed non-editable (`python -m pip install --user .`), so one project's problem, or the harness building itself, never changes the code other projects run. After a harness sprint passes and its checks are green: bump `harness/__init__.py`, commit, push, `python -m pip install --user --force-reinstall --no-deps .`, then copy `%APPDATA%/Python/Python312/Scripts/harness.exe` to `~/.local/bin/`.
- With the non-editable install the harness can drive its own sprints directly; no frozen venv needed.
- Code map (v0.14): no switches. Every agent is offered the map and decides itself; the harness updates the project map once per stage and copies it, plus Graphify's own rules file, into each task worktree. `graphify-out/` is excluded through `.git/info/exclude`.
