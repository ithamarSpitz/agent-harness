---
id: S9-T16
stage: 2
model_class: hard_worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S9-T15
allowed_paths:
  - harness/impact.py
  - harness/prompts.py
  - harness/provider_runner.py
  - tests/harness/test_impact.py
  - tests/harness/test_review_prompt_impact.py
context:
  - spec
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

<!-- The harness hands the reviewer the callers of what changed -->

# Goal

The review prompt already lists the code outside the diff that uses what the task changed, so the reviewer does not have to choose to look it up.

# Background

Agents rarely use optional tools: in S8 the code map was used in 0 of 155 reviews (S9-T15). The project map at `graphify-out/graph.json` (copied into each worktree when `code_map` is enabled) has nodes with `source_file` and links with `relation` (`calls`, `imports_from`, `uses`, `references`, `method`, ...) and `source_file` / `source_location` (e.g. `L8`).

# Decisions (do not reopen)

1. New `harness/impact.py`: from the task diff (`git diff <base_ref>` in the worktree) find the changed Python functions, classes and methods (by the hunk line ranges and `ast` of the changed files), then read `graphify-out/graph.json` and list every `calls`, `imports_from`, `uses` or `references` link to them from files outside the diff, as `file:line -> symbol`. Cap at 40 lines plus "and N more".
2. Without a map, or if reading it fails, fall back to a word-boundary `git grep` of each changed name outside the changed files (same cap). Never fail the review because of this step. A map that cannot be read logs one warning through the module logger (never a print to stderr) and the list header says `code map unreadable, used grep`.
5. Operator decisions after a real run (Nov 3 2026, do not reopen): the grep fallback searches only `*.py` files, skips the files changed in the diff, lists at most 5 users per changed symbol (so one common name like `run` cannot fill the list), and lists `harness/` users before `tests/`. The graph path keeps the 40-line cap only.
6. The previous attempt committed three reviewer scratch files at the repository root (`_graph_query_tmp.py`, `_tmp_graph_probe.py`, `_tmp_parse_graph.py`). Deleting them is required by the operator and is not an allowed_paths violation (they are not in the base the task started from).
3. Review prompts get a section "Code outside the diff that uses what changed:" with that list (or "none found"), placed before the S9-T15 impact step. Built in `ProviderAgentRunner` for every provider's review call.
4. Standard library only; reading the map is a JSON load, no graphify process.

# Acceptance criteria

1. A fixture repo where a task changes `f()` and another file calls it: the review prompt lists that caller, from a fake `graph.json` and again with no map (grep fallback).
2. A broken `graph.json` falls back to grep with one logged warning and the `code map unreadable, used grep` header.
5. A README and a changed file that mention a changed symbol are not listed; a symbol used in more than 5 places lists 5.
3. More than 40 users are capped with the count.
4. Implementation prompts are unchanged.

# Rules for every S9 task

- Standard library only (plus what the harness already depends on). Windows, Linux and macOS.
- Tests never call real agents (Codex, Cursor, Claude Code), real GitHub, or the network. Use fakes and recorded samples.
- Never read, print, log or store tokens or credentials. Claude Code uses the user's own login; the harness only starts the `claude` program.
- Statuses and waits use the S7 names (`WAITING_QUOTA`, `WAITING_NETWORK`, ...).
- Change only `allowed_paths`. If a criterion cannot be met inside them, stop and report it in OPEN_ISSUES.
