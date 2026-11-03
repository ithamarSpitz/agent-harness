---
id: S9-T24
stage: 4
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/scheduler.py
  - tests/harness/test_brief_reload.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- Brief fixes apply to the next attempt; an agent that reports blocked is not retried blindly -->

# Goal

When the operator fixes a task brief during a run, the next attempt of that task uses the fixed brief, and attempts are not spent repeating something the agent already reported as impossible.

# Background

On Nov 3 2026 S9-T10 and S9-T18 needed one more test file in `allowed_paths`. The operator fixed both briefs, but the running scheduler keeps the `TaskSpec` it loaded at start, so the remaining attempts were verified against the old `allowed_paths` and failed the same way; S9-T10's Codex attempts 2 and 3 even reported `STATUS: blocked` with the exact reason in `OPEN_ISSUES`, and were retried anyway. Each wasted attempt costs a full agent call plus a full test run. Prompts already embed the current brief text (S8-T36); only the scheduler's own copy of the frontmatter is stale.

# Decisions (do not reopen)

1. Before each implementation attempt (the attempt loop in `_run_task_inner`), re-read the task's brief file and take the current `allowed_paths`, `verification`, `timeout_minutes`, `review`, `allow_protected` and `context` from it. `id`, `stage` and `depends_on` changes are ignored until the next start, with one log line saying so. If the file cannot be read or parsed, keep the previous spec and log one warning.
2. When an implementation attempt's report says `STATUS: blocked` and its verification then fails, do not start another attempt: the task becomes `BLOCKED` at once with the agent's `OPEN_ISSUES` text in `last_error` (the operator decides). A report without `STATUS: blocked` keeps today's retry behavior.
3. Log each reload that changed a field, naming the fields.

# Acceptance criteria

1. A fake run where the brief's `allowed_paths` gains a file between attempt 1 and 2: attempt 2 is verified with the new list and passes.
2. A fake agent that reports `STATUS: blocked` with failing verification: the task is BLOCKED after that attempt with the OPEN_ISSUES text, and no further agent call is made.
3. A broken brief file between attempts keeps the old spec and logs one warning.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
