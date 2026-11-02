---
id: S6-T4
stage: 2
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S6-T3
allowed_paths:
  - harness/init_project.py
  - harness/cli.py
  - tests/harness/conftest.py
  - tests/harness/test_init_project.py
context: []
environment: null
verification:
  - python -m pytest -q tests/harness
  - python -m ruff check harness tests
  - python -m mypy harness
---

# Goal

`harness init` always starts a project as its own Git repository with a GitHub remote, and gives it an architecture document that the plan and every task brief build on.

# Decisions (do not reopen)

1. **Git.** If the root is not a Git repository, run `git init -b main` instead of today's warning.
2. **GitHub.** If the repository has no `origin` remote, create one with `gh repo create <name> --private --source . --remote origin` where `<name>` is the root directory name. Options: `--public` (create it public) and `--no-github` (skip). If `gh` is missing or not logged in (`gh auth status` fails), print a warning with the exact command to run later and continue; never fail init for this.
3. **First commit.** When init created the repository (decision 1), commit the generated files as `Initial harness setup` and, when `origin` exists, push `main` with upstream (`git push -u origin main`). In an existing repository with history, do not commit; print that the files are ready to commit.
4. **Architecture template.** Create `ARCHITECTURE.md` when missing, with these sections and a one-line hint under each: Goal and users; Scope and non-goals; Stack; Components (what each does and owns); Data model; Interfaces and contracts between components; Testing strategy (which commands prove what); Decisions (dated, with the reason). Never overwrite it, even with `--force`.
5. **Wiring.** The generated `harness.yaml` adds `ARCHITECTURE.md` to `paths.protected` and the alias `architecture: ARCHITECTURE.md` under `context.aliases`. The generated example brief lists `architecture` in `context`, and the task template explains that briefs name the architecture sections they rely on.
6. The options `--public` and `--no-github` are added to the `init` subcommand in `harness/cli.py`.
7. Output lines stay as today (`created` / `updated` / `skipped (exists)`) plus one line each for git, GitHub and the first commit.

# Test safety (added after a real incident, do not reopen)

During this task's verification, tests called the real, logged-in GitHub CLI and created 8 real repositories on the user's account, one of them public. Tests must never be able to reach real GitHub:

1. Add an autouse fixture in `tests/harness/conftest.py` that puts a temporary directory first on `PATH` containing a fake `gh` (a `gh.cmd` on Windows and an executable `gh` script on POSIX) that prints `real gh is blocked in tests` and exits 1. Tests that need `gh` put their own fake before it.
2. Every test of `harness init` either passes `--no-github` or installs a fake `gh` that records its arguments; tests assert on those recorded arguments.
3. Pushes in tests go only to a local bare repository created in the test's temp directory.
4. Add a test proving that with no test fake installed, `harness init` cannot reach the real `gh` (the blocking fake is what runs).

# Acceptance criteria

Tests use temp directories and a fake `gh` command (a small Python script that records its arguments and can simulate "not logged in"):

1. Empty folder: a repo is initialized on `main`, `gh repo create <name> --private --source . --remote origin` is called, the files are committed, and push is attempted.
2. `--no-github`: no `gh` call; `--public`: `--public` instead of `--private`.
3. `gh` missing or not logged in: warning with the command, init still succeeds and commits.
4. Existing repo with history and an `origin`: no `gh` call, no commit, no push.
5. Existing `ARCHITECTURE.md` is kept, also with `--force`; `harness validate` passes on the fresh project.
