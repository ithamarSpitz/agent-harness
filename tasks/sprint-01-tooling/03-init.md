---
id: S1-T3
stage: 3
model_class: worker
timeout_minutes: 40
review: true
allow_protected: false
depends_on:
  - S1-T2
allowed_paths:
  - harness/**
  - tests/harness/**
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

`harness init` sets up a new project for the harness in one command, so a new project needs no copying from an old one.

# Decisions (do not reopen)

1. **Command.** `harness init [--force]`, acting on the existing `--root` (default: current directory). Code in a new module `harness/init_project.py`, wired into `harness/cli.py`.
2. **Templates are Python string constants** in `harness/init_project.py` (no package data files; `pyproject.toml` stays unchanged).
3. **Files it creates when missing:**
   - `harness.yaml`: every section the config supports, with today's defaults and a one-line comment per section. Cursor models use only Cursor-native classes: `worker: composer-2.5`, `hard_worker: cursor-grok-4.6-high`, `reviewer: cursor-grok-4.6-high`, and `retry.sequence: [worker, hard_worker, hard_worker]`. `code_map.enabled: false` and `ui_review.enabled: false` with their keys present and commented. `paths.protected` contains `harness.yaml`, `tasks/**` and `AGENTS.md`. `verification.global_task_commands: [git diff --check]`.
   - `tasks/roadmap.yaml`: `project: <root directory name>` and one example sprint with one task pointing at the example brief below.
   - `tasks/TASK_TEMPLATE.md`: the frontmatter keys the harness supports (including `code_map`) with comments, and the brief sections Goal, Scope, Out of scope, Acceptance criteria.
   - `tasks/sprint-01-example/01-example.md`: a minimal valid brief, so `harness validate` passes right after init.
   - `AGENTS.md`: when missing, a short agent contract (work only on the task, stay in `allowed_paths`, do not commit, the final STATUS / CHANGED_FILES / TEST_RESULTS / OPEN_ISSUES report). When it exists, append the same rules between `<!-- harness:start -->` and `<!-- harness:end -->` markers, only if the markers are not there yet.
   - `.gitignore`: append the lines `.harness/` and `graphify-out/`, each only if absent; create the file when missing.
4. **Never overwrite** existing `harness.yaml`, `tasks/roadmap.yaml` or `tasks/TASK_TEMPLATE.md` unless `--force` is given. `AGENTS.md` and `.gitignore` are only ever appended to, even with `--force`.
5. **Output.** One line per file: `created`, `updated` (appended) or `skipped (exists)`, then a next-steps line: edit `harness.yaml` verification commands, write briefs, run `harness doctor` and `harness validate`.
6. If the root is not a Git repository, print a warning and still create the files.
7. **README.** Replace any instructions that tell users to copy files from another project with `harness init`.

# Out of scope

Interactive questions, detecting the project's language or commands, creating Git repos, installing tools, any change to Graphify or UI-review behavior.

# Acceptance criteria

1. In a fresh temp Git repo: `harness init` creates all files; `HarnessConfig.load` reads the new `harness.yaml`; `harness validate` passes.
2. A second `harness init` changes nothing and reports every file as skipped (AGENTS.md markers present, `.gitignore` lines present).
3. An existing `AGENTS.md` keeps its content and gets the marked block once; an existing `.gitignore` gets no duplicate lines.
4. `--force` rewrites the three template files and still only appends to `AGENTS.md` and `.gitignore`.
5. The generated `harness.yaml` round-trips: every key in it is a known config field (loading it raises no unknown-key error).
