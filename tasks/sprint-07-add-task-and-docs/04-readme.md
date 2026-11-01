---
id: S7-T4
stage: 4
model_class: worker
timeout_minutes: 45
review: true
allow_protected: false
depends_on:
  - S7-T5
allowed_paths:
  - README.md
  - CHANGELOG.md
context: []
environment: null
verification:
  - >-
    python -c "import pathlib,sys;r=pathlib.Path('README.md').read_text(encoding='utf-8');c=pathlib.Path('CHANGELOG.md').read_text(encoding='utf-8');bad=[];bad+=['README too long'] if len(r.splitlines())>250 else [];bad+=['README must link SPEC.md'] if 'SPEC.md' not in r else [];bad+=['CHANGELOG empty'] if len(c.strip())<200 else [];sys.exit('; '.join(bad) if bad else 0)"
---

# Goal

Turn `README.md` from an 800-line changelog into a short, current guide, and move the history into `CHANGELOG.md`. A separate task in the same stage writes `SPEC.md` (the full behavior description); the README links to it instead of repeating it.

# Decisions (do not reopen)

1. **README.md** (at most 250 lines), sections in this order:
   - What the harness is (5 lines) and a link to `SPEC.md` for how it works in detail.
   - Install on a new computer: base tools and logins the user must do themselves (Git, Python 3.11+, Node.js, GitHub CLI, Codex CLI, Cursor CLI; `gh auth login`, `codex login`, `agent login`), then clone and run `scripts/install.ps1` or `scripts/install.sh`.
   - Start a new project: `harness init` (new Git repo and private GitHub repo, `ARCHITECTURE.md` template), then write the architecture, the roadmap and the briefs; how a good brief looks (allowed paths, closed decisions, acceptance criteria, verification commands), with one short example.
   - Run a sprint: `validate`, `graph`, `run --dry-run`, `doctor`, `run`, `status`, `resume`; what to do when a task is BLOCKED (`failure`, `rerun-blocker`, `run-task --fresh-budget`).
   - Command list: every subcommand with one line each (take the list from `harness --help`).
   - Usage and limits: `harness limits` / `usage`.
   - Developing the harness itself: it builds itself with its own roadmap; tests and checks; releases install automatically after a successful sprint.
2. Every command and option named must exist in `harness --help` / the code. No version-by-version notes in the README.
3. **CHANGELOG.md**: move the version history from the old README (newest first), and add entries for the work in `tasks/roadmap.yaml` sprints S1 to S7 (one or two lines each, taken from the sprint names and briefs). Drop project-specific instructions for other projects (NYC, BookNook); keep only what changed in the harness.

# Acceptance criteria

1. README is at most 250 lines and links `SPEC.md`; CHANGELOG holds the history.
2. Every command in the README exists.
3. Nothing from the old README that is still true and useful to a user is lost: it is either in the README, in CHANGELOG, or covered by SPEC.md.
