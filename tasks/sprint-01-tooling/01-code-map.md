---
id: S1-T1
stage: 1
model_class: worker
timeout_minutes: 45
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

Give agents an optional code map built with Graphify (`pip install graphifyy`, command `graphify`), so they can find code without broad searching. The purpose is to spend fewer tokens. A map is built only for tasks that ask for it, and only when the code changed since the last build.

# Facts about Graphify (verified with version 0.9.100)

- `graphify update . --no-cluster`, run in a directory, builds a code-only graph with no LLM and no network and writes `graphify-out/graph.json` (plus `graphify-out/cache/` and `graphify-out/manifest.json`) in that directory. It takes about 15 seconds on this repo.
- `graphify explain "<symbol or file>" --graph <path>` prints one node and its connections: about 150 tokens.
- `graphify path "<A>" "<B>" --graph <path>` prints the shortest path between two nodes: small.
- `graphify query "<question>" --graph <path>` can print 15,000 tokens or more even with its default budget. Agents must not use it.
- `graphify --help` exits 0 when installed.

# Decisions (do not reopen)

1. **Config.** A new `code_map` section in `harness.yaml`, a `CodeMapConfig` dataclass with these defaults: `enabled: false`, `command: graphify`, `default_for_tasks: false`, `build_timeout_minutes: 5`. A missing section keeps today's behavior.
2. **Per task.** New optional task frontmatter key `code_map: true | false`, added to `TaskSpec` and to `schemas/task-frontmatter.schema.json`. A task uses the map when `code_map.enabled` is true and the task's value (or `default_for_tasks` when the task sets nothing) is true.
3. **Build only when the code changed.** The cache key is the commit the task worktree was created from (its base commit SHA). The map lives at `<runtime root>/code-map/<base sha>/graph.json` (the runtime root is the harness's `.harness/` directory). Before the first agent call of a task that uses the map:
   - if that file exists, reuse it and build nothing;
   - otherwise run `<command> update . --no-cluster` in the task worktree (after worktree setup commands), copy `graphify-out/graph.json` to the cache path atomically (temp file, then `os.replace`), and delete the worktree's `graphify-out/` directory so it never shows up as a changed file.
   Retries and the review of the same task reuse the same map. Two parallel tasks with the same base may both build; the atomic write makes that harmless. No locks.
4. **Never fatal.** If the command is missing, fails or exceeds `build_timeout_minutes`, write one warning line to the task's log and continue without a map. The task must not fail because of the map.
5. **Prompts.** `implement_prompt` and `review_prompt` get an optional code-map path. When it is set, add exactly this block (with the configured command and the absolute path filled in), and nothing when it is not set:

   ```text
   Code map: <path>
   To locate code, use `<command> explain "<symbol or file>" --graph "<path>"` or `<command> path "<A>" "<B>" --graph "<path>"`. Their output is small.
   Do not use `<command> query` (very large output) and do not open the graph file. Use the map only when you need to find where something is defined or used.
   ```

   The plan-repair prompt does not change.
6. **Usage records.** Each usage record the harness writes gets a boolean `code_map` field (whether that call had a map), so token use with and without the map can be compared later.
7. **Doctor.** When `code_map.enabled` is true, `doctor` runs `<command> --help`; on failure it reports an error with the hint `pip install graphifyy`. When disabled, doctor does not mention Graphify.
8. **README.** A short "Code map (Graphify)" section: what it is, how to enable it (config + task key), that it is rebuilt only when the base commit changes, and the agent rules from decision 5.

# Out of scope

Running Graphify on a schedule or with `watch`, git hooks, Graphify's own `install` integrations, LLM or semantic extraction, `GRAPH_REPORT.md`, any UI or Playwright work, `harness init`.

# Acceptance criteria

1. With no `code_map` section, behavior and prompts are unchanged; all existing tests pass unmodified.
2. Tests with a fake `graphify` command (a small Python script that writes `graphify-out/graph.json` and counts its own calls) prove:
   - a task with `code_map: true` gets the map block in its implement and review prompts, with the cache path;
   - two tasks (or two attempts) on the same base commit call the build once;
   - a new base commit triggers a new build;
   - the task worktree has no `graphify-out/` and no extra changed files after the build;
   - a failing or missing command leaves the task running without the map block;
   - a task with `code_map: false`, or with the section disabled, never calls the command.
3. Doctor tests cover enabled-and-working, enabled-and-missing, and disabled.
4. Usage records include the `code_map` field.
