# Harness operator handbook

For whoever drives `harness.py` (usually Claude). Read before starting, resuming or repairing a run. Every rule here came from a mistake that cost real tokens or quota on the BookNook build (Oct 2026). `README-HARNESS.md` explains what the harness does; this file is how to operate it without wasting attempts.

## 1. Golden rules

1. **The integration checkout stays clean, and changes to it happen only while no `harness.py` process runs.** No untracked files ever. Commit (brief fixes, harness fixes) only when the harness is idle, for example after it stopped on a blocked task. Since the base-ref fix, existing worktrees are diffed against their own base, so such commits no longer show up as task changes. A task worktree keeps its old copy of `tasks/` and docs: to give a kept worktree a changed brief, fast-forward it (`git -C <worktree> merge --ff-only build`) and set that task's `base_ref` in `.harness/state.json` to the new head.
2. **Logs go outside the repo.** Redirect `run`/`resume` output to the scratchpad, never to `.harness/` or anywhere tracked. An untracked log made the first S0 run refuse to start.
3. **Never pay for the same diagnosis twice.** When a task fails, read the evidence (section 4) before any retry. A retry without a changed input repeats the failure.
4. **Operator mistakes look like agent failures.** A scope or protected-path violation on a file the task never touches (for example `harness.yaml`) is almost always caused by the operator, not the agent. Check what you changed first.

## 2. Before a run

- `python harness.py validate`, `graph <SPRINT>`, `run <SPRINT> --dry-run`, `doctor --full`.
- `git status --short` must be empty.
- **Memory.** Check free RAM. Worktree setup (`npm ci`) runs once per parallel task at the same moment, on top of Docker and Supabase. With under about 6 GB free, use `max_parallel_agents: 2`. Claude Code kills background shells under memory pressure; the run then needs `resume`. It happened again at 2 agents with about 2 GB free (Docker/WSL 3 GB plus Chrome). After a kill: check for orphaned agent processes (`Get-CimInstance Win32_Process` filtered on `worktrees`), then `resume`; the task left `RUNNING` continues in its worktree. Ask the user to start Claude Code with `CLAUDE_CODE_DISABLE_BG_SHELL_PRESSURE_REAP=1` for long runs.
- **Quota.** Ask the user for, or check, Codex's 5-hour and weekly limits and Cursor usage. One blocked task (3 attempts plus reviews) can burn a 5-hour Codex window.
- **Cursor ladder stays native.** `retry.sequence` and `plan_repair.planner_model_class` use only Composer and Grok classes. Opus bills as "Other Models" or on-demand spend.
- **Stray processes.** Look for old dev servers (`supabase functions serve`, web servers) that would collide with verification ports, and stop them before a task that starts its own.
- **Tooling that verification relies on must work from a fresh clone.** If code imports packages (Deno `npm:` specifiers), the repo must resolve them itself (`deno.json` with `"nodeModulesDir": "auto"`). Turn off lock files that verification would rewrite (`"lock": false`), or the barrier finds a dirty checkout.

## 3. While it runs

- Start with the Bash tool's `run_in_background`; you are notified when the process exits. Do not poll.
- One Monitor as a fallback, re-armed every 30 minutes. It reads `harness.py status` and reports a new `WAITING_FOR_QUOTA` / `WAITING_FOR_CAPACITY` or 60 minutes without a `state.json` change. Do not grep `state.json` for `WAITING`: leftover fields gave false alarms.
- Codex prints quota errors with a typographic apostrophe ("You’ve hit your usage limit"). Patterns now normalize it; if a quota looks like a capacity wait again, check the raw `.harness/logs/<TASK>-attempt-N-codex.log`.
- Provider fallback is per task: each new task tries Codex first, a task that hits a quota stays on Cursor for its own retries and review.

- After an `Agent stalled` or timeout attempt, check that only one agent process works in that worktree (filter `Win32_Process` on the worktree path; compare start times). Killing processes may need the user's approval in auto mode.

## 4. Reading a failure (cheap first)

1. `python harness.py status <SPRINT>` and `failure <SPRINT>`.
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
- **`harness.py verify <TASK>` on Windows:** set `PYTHONIOENCODING=utf-8` when redirecting output, or Hebrew text crashes the print with a `charmap` error.

## 6. Writing briefs and contracts

- Give every file exactly one owner per stage. Before a sprint, check each brief's scope against the contract: if a parallel task needs a helper in a file another task owns, either give it a local copy (and let the integration gate consolidate) or move it to the contract task.
- Spec-type tasks need an explicit quality bar in the brief: self-contained (restate rules instead of "see architecture"), field-to-column tables, typed signatures, edge-case rules. Back the checkable parts with a script (`scripts/check-contract.mjs`).
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

These fixes live in `torimitor/harness/`. Port them back to `dev/nyc-property-resolver` (or the standalone harness) before reusing it elsewhere.
