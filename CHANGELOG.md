# Changelog

Newest first. Harness behavior details live in [SPEC.md](SPEC.md); the [README](README.md) is the short operator guide.

## Sprint S9 — Claude Code, headroom routing, impact review, and model outcomes

- Claude Code provider with implementation/review ladders, quota/error handling, Playwright support, and configurable five-hour/weekly reserves.
- Machine-wide headroom routing by urgency and expiring-quota waste, Cursor Auto only at 100%/100%, and reviews assigned away from the implementer when possible.
- Required review impact analysis with outside-diff caller/importer context and a grep fallback.
- Machine-wide `HARNESS_HOME` outcomes, per-call verification/result fields, final task records, and `harness models report|import`.
- Operator `hold`/task `release`, per-attempt brief reload, and agent-reported blocks that stop retries.

## Sprint S8 — Memory-aware scheduling, live board and console, safer execution

- Live ANSI board for `run`/`resume`, attach-only `harness watch`, persistent event feed, `--no-board`, and an interactive console for inspection, task queueing, retry, and drain-and-pause stop requests.
- Machine-wide agent ledger, free-memory admission with fair sharing between projects, learned per-agent memory peaks, and parallel-limit fallback when memory cannot be read.
- In-stage capacity retries, reset-aware quota sleeps, distinct transient wait causes, launch/resource failure handling, verification process-tree timeouts, and corrected Cursor stream-json review verdicts.
- Current briefs embedded in agent prompts, absolute internal brief paths with relative operator output, stronger worktree validation/cleanup, and Git-excluded code-map caches.
- Playwright UI checks for implementers and reviewers on both providers with scoped process/port cleanup after calls.
- Optional self-host restarts on newly integrated code and an atomic semantic-version bump at this repository's release start.

## Sprint S7 — Add tasks to a running sprint, agent activity records, SPEC and README

- `harness add-task` queues briefs during a run; successful sprints record added tasks in Git.
- Agent activity records for implement/review attempts.
- Task status and stall/resume/review-only behavior aligned with the scheduler.
- Consistent stage merging; Cursor errors classified for recovery.
- **SPEC.md** documents full harness behavior; **README.md** shortened with history moved here.

## Sprint S6 — Live Claude limits, faster tests, machine setup, init creates the repo

- `harness limits` shows live Claude usage from the OAuth usage API (status-line cache as fallback).
- Faster harness test suite defaults.
- `harness setup-machine` and install scripts for new machines.
- `harness init` creates the Git repo, GitHub repo, and starter architecture/tasks.

## Sprint S5 — Release after a successful sprint; live Codex limits

- Optional `release` commands after a successful sprint; `harness release` to retry failures.
- `harness limits` reads live Codex rate limits when the app-server API is available.

## Sprint S4 — harness limits

- `harness limits` / `--json`: Codex, Cursor, and Claude usage in one command.

## Sprint S3 — Code map for every agent

- Graphify code map built once per stage and offered in each task worktree; reviewer port cleanup safe across projects.

## Sprint S2 — Printed commands use the installed harness command

- All harness messages and hints use `harness <subcommand>` instead of `python harness.py`.

## Sprint S1 — Graphify, Playwright UI review, harness init

- Optional Graphify code map integration and Playwright MCP UI review for independent reviewers.
- `harness init` scaffolds a new project (expanded in later sprints).

---

## v0.12.5 — Codex implementation profiles

Codex implementation routing uses pinned Sol/Astra profiles per attempt; review and plan-repair keep existing Codex settings. Explicit Codex quota/auth/model-unavailability switches the current task to the Cursor ladder without consuming a provider-switch attempt; capacity/network failures stay on Codex with the capacity wait path.

## v0.12.4 — Codex-first provider routing

Codex CLI is the primary provider when authenticated. Explicit Codex quota/auth/model-unavailability switches the run to the Cursor Composer/Grok/Opus ladder without consuming a provider-switch attempt. Reviews and plan-repair prefer Codex with Cursor fallback. `harness usage` separates providers. Optional `codex.windows_implementation_sandbox` for native Windows implementation calls in harness worktrees only.

## Per-task provider fallback

Codex quota/auth/model-unavailable results switch only the current task to Cursor (per-task provider disable state). Each new task probes Codex first. Quota phrases match typographic apostrophes in Codex messages.

## v0.11.6 — Deterministic verification environment

Explicit task/stage verification environments are authoritative and no longer re-merge ambient `os.environ`. `verification.env` remains fallback-only; real task/runtime values win.

## v0.11.5 — Provider capacity and verification-env fix

Cursor `resource_exhausted` / overload is temporary provider capacity, not quota exhaustion. Capacity waits preserve worktree state and do not consume implementation retry budget. Review resumes from verification/review after capacity waits. `harness quota status` and `harness usage` distinguish quota vs capacity pauses. Verification subprocesses get validation-only `DATABASE_URL` fallback when unset; real env values always win.

## v0.11.4 — Windows Cursor prompt transport

On Windows, long prompts stream over stdin for the official `agent.cmd`/`.bat` launcher so batch/PowerShell chains do not corrupt prompts or drop `--trust` / `--force`. Probe script for minimal Cursor smoke in a harness worktree.

## v0.11.3 — Diagnostics state cleanliness

`.harness/diagnostics/` is ignored so forensic archives do not dirty the integration checkout or fail `harness doctor --full`.

## v0.11.2 — Windows headless execution

Implementation calls add Cursor `--force` only in verified harness-owned worktrees. Review and plan-repair stay read-only/unforced. `reset-task` clears the task from the active sprint failure record.

## v0.11.1 — Windows portability

Cursor/other `.cmd`/`.bat` shims launch through `cmd.exe` on Windows. Python-script commands use the active interpreter. Docker Compose uses the same cross-platform launcher. Test fixtures avoid Unix-only shell assumptions.

## v0.6.2 — Automatic model escalation

Deterministic implementation model sequence per attempt with fresh invocations; manual `run-task --model-class escalation` is one-shot.

## v0.6.1 — Windows unattended-run hardening

- `--trust` only for Git worktrees directly under `.harness/worktrees`.
- Nested keep-awake for full live `run`/`resume` including quota waits.

## v0.5 — Plan repair and quota-safe continuation

- `harness plan-change propose|status|apply|reject` for human-gated roadmap/architecture repairs.
- Quota exhaustion → `WAITING_FOR_QUOTA` without consuming retry budget; `harness quota status|set-reset|clear-reset`.
- Provider capacity → `WAITING_FOR_CAPACITY` with shorter retry cadence.
- `verification.env` fallback-only semantics.

## v0.3 — Docker environments and focused failure recovery

- Durable failure reports under `.harness/failures/`; `harness failure`, `harness rerun-blocker`, `harness run-task --fresh-budget`.
- Opt-in per-task `environment: docker` with isolated Compose project names.
- Stage barrier rollback semantics documented; focused barrier retry without re-invoking agents.

## v0.2 — Pre-task hardening

- `harness validate` for roadmap/task lint before model calls.
- `harness doctor` / `harness doctor --full` preflight.
- Stall watchdog configurable via `execution.stall_timeout_minutes`.
- Run manifests under `.harness/runs/<run-id>/`; `harness manifest`.
- Recovery: `inspect`, `unblock`, `reset-task`, `cleanup`.
- Logical context aliases in `harness.yaml`; `harness usage` local invocation log.

## Initial harness (v0.1 baseline)

- Sprint/stage/task roadmap, dependency validation, parallel stages.
- One Git worktree per task; durable `.harness/state.json`.
- Retry budgets, timeouts, allowed/protected paths, mechanical verification, independent review.
- Atomic stage integration barrier with rollback on merge or stage-verification failure.
- CLI for status, graph, run, resume, verify, review, cleanup, doctor.
- Per-task Docker/Compose isolation when enabled; agents do not commit—harness owns merges.
