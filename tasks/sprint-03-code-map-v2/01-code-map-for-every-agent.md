---
id: S3-T1
stage: 1
model_class: hard_worker
timeout_minutes: 50
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - harness/**
  - tests/harness/**
  - schemas/task-frontmatter.schema.json
  - README.md
context:
  - readme
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

Replace the switch-based code map from S1-T1 with a simpler model: **every agent is offered the code map, and the agent itself decides whether to use it.** Nobody decides per task or per project. Also make the reviewer's port cleanup safe when several projects run at once.

# Background (read this before touching code)

`harness/code_map.py` today builds a Graphify map only when `code_map.enabled` is true and the task has `code_map: true`, caches one `graph.json` per base commit in `.harness/code-map/<sha>/`, and adds a 3-line block to the prompts. This task removes both switches, keeps one incrementally updated map per project, gives each task worktree its own copy that the agent may update, and teaches the agent with Graphify's own rules file.

Facts about Graphify 0.9.100 (verified):

- `graphify update <dir> --no-cluster` builds or incrementally updates a code-only graph (no LLM, no network) and writes `<dir>/graphify-out/` (`graph.json`, `manifest.json`, `cache/`). With an existing `graphify-out/` it re-reads only changed files.
- Commands an agent uses: `graphify query "<question>" --budget N` (usually 1,500 to 2,000 tokens; about 500 with `--budget 500`), `graphify explain "<symbol or file>"` (about 150 tokens), `graphify path "<A>" "<B>"`. They read `graphify-out/graph.json` in the current directory by default.
- The installed package ships an agent rules file at `<graphify package dir>/always_on/agents-md.md` (about 1.5 KB). It tells agents how to use query/explain/path and to run `graphify update .` after modifying code.

# Decisions (do not reopen)

1. **No switches.** Remove `code_map.enabled`, `code_map.default_for_tasks`, the task frontmatter key `code_map` (from `TaskSpec`, `roadmap.py`, the schema, the `harness init` templates and the README). The `code_map` config section keeps only settings: `command` (default `graphify`) and `build_timeout_minutes` (default 5). Old files that still contain the removed keys must load without error: ignore those keys (in config and in task frontmatter).
2. **Availability.** Graphify is available when `<command> --help` exits 0. Probe once per harness process and remember the result. When it is not available: no map, no prompt lines, no error, and `doctor` shows a **warning** (not required, not a failure) with the hint `pip install graphifyy`.
3. **One project map, kept current by the harness.** The project map lives in a harness-owned detached Git worktree at `<runtime root>/code-map/source` (runtime root = `.harness/`). Before the tasks of each stage start, if the map's recorded commit differs from the stage base commit: check out the base commit there (`git checkout --detach <sha>`), run `<command> update . --no-cluster` there (incremental thanks to the kept `graphify-out/`), and record the commit in `<runtime root>/code-map/commit.txt`. Run it once per stage in the scheduler, before the stage's tasks are launched, so parallel tasks never build at the same time. Delete the old per-commit cache layout (`<runtime root>/code-map/<sha>/`) if present.
4. **Each task gets its own copy.** After worktree setup and before the first agent call, copy the project map's `graphify-out/` (all of it, including `cache/` and `manifest.json`) into the task worktree's `graphify-out/`, and copy Graphify's rules file there as `graphify-out/AGENTS-graphify.md`. Find the rules file with `importlib.util.find_spec("graphify")`; if it cannot be found, write this fallback text instead:

   ```text
   Use `graphify query "<question>" --budget 500`, `graphify explain "<symbol or file>"` and `graphify path "<A>" "<B>"` to find code and see what depends on what. Read only the source lines the map points to. After you change code, run `graphify update .` so the map shows your changes.
   ```

   An agent may run `graphify update .` in its worktree whenever it wants; that only changes its own copy.
5. **Never a changed file.** Add `graphify-out/` to the Git common directory's `info/exclude` (shared by all worktrees of the repo) once, idempotently, before copying. Changed-file scope checks, the reviewer worktree snapshot and the harness commit must never see `graphify-out/`. Do not edit the project's `.gitignore`.
6. **Prompts.** When a map was copied into the worktree, `implement_prompt` and `review_prompt` contain exactly this block, and nothing about the map otherwise:

   ```text
   A code map of this repository is available in graphify-out/. You decide whether it helps. If you use it, first read graphify-out/AGENTS-graphify.md. Add `--budget 500` to `graphify query`.
   ```

   Use the configured command name in place of `graphify` when it differs. The plan-repair prompt does not change.
7. **Never fatal, never blocking.** Any failure in decisions 3 to 5 (missing command, failed or timed-out build, copy error) writes one warning line to `<runtime root>/logs/code-map.log` or the task's code-map log and continues without a map. It must not block a task, a stage or the sprint. A timed-out build is stopped with `stop_process_tree`.
8. **Usage records** keep the boolean `code_map` field, now meaning "the map was offered to this call".
9. **Reviewer port cleanup must not touch other projects.** In `cleanup_ui_review_processes` (`harness/ui_review.py`), record the processes listening on `ui_review.ports` before the review starts, and after the review stop only listeners that were **not** listening before. Processes whose command line contains the task worktree path are still stopped as today.
10. **README.** Replace the S1 code-map section: every agent is offered the map and decides itself; the harness keeps the project map current once per stage; each task has its own copy the agent may update; Graphify's own rules file teaches the agent; nothing to configure except `command` and `build_timeout_minutes`; works without Graphify.

# Out of scope

Graphify `install`, hooks, `watch`, MCP, LLM or semantic extraction, reports or wiki generation, changes to UI-review behavior other than decision 9, changes to `harness.yaml` or `tasks/**`.

# Acceptance criteria

Tests with a fake `graphify` command (a small Python script that writes `graphify-out/graph.json`, `manifest.json` and `cache/`, and counts its calls and whether `graphify-out/` already existed) prove:

1. A task gets the prompt block and a worktree `graphify-out/` containing the map and `AGENTS-graphify.md`, with no switch set anywhere.
2. Two tasks in the same stage cause one project-map update; a new stage base causes one more, and that update runs with the existing `graphify-out/` (incremental).
3. `git status --porcelain` in the task worktree does not show `graphify-out/`, and the harness commit does not contain it.
4. With the command missing or failing, tasks run with no map and no prompt block, and the sprint is not blocked; doctor reports a warning, not a failure.
5. A `harness.yaml` with `code_map: {enabled: true, default_for_tasks: false}` and a brief with `code_map: true` both still load.
6. Rules-file fallback text is used when `find_spec("graphify")` returns None.
7. Port cleanup: a listener that existed before the review is not stopped; a new one is.
