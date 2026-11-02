# Agent Harness

A small orchestrator that runs Codex and Cursor coding agents against a Git repository using Markdown/YAML task briefs and a sprint roadmap. It keeps project knowledge in Git instead of a separate memory system. For full behavior, configuration, and guarantees, see [SPEC.md](SPEC.md).

## Install on a new computer

Install and authenticate these yourself before using the harness:

- **Git**
- **Python 3.11+**
- **Node.js** (required by some agent CLIs and project setups)
- **GitHub CLI** (`gh`) — run `gh auth login`
- **Codex CLI** — run `codex login`
- **Cursor CLI** (`agent`) — run `agent login`

Clone this repository, then from the repo root run:

```bash
# Windows
powershell -ExecutionPolicy Bypass -File scripts/install.ps1

# macOS / Linux
sh scripts/install.sh
```

That installs the `harness` command for your user and runs `harness setup-machine` for local paths and tooling checks.

Optional: create a venv and `pip install -r requirements-harness.txt` if you develop from a checkout without installing the package.

Check the environment:

```bash
harness doctor --full
```

Provider enablement is in `harness.yaml` (`codex.enabled`, `cursor.enabled`). See [SPEC.md](SPEC.md) for routing, fallback, and trust rules.

## Start a new project

From an empty directory or an existing app repo:

```bash
harness init
```

This creates a new Git repository (when needed), a private GitHub repository via `gh`, starter files including `harness.yaml`, `tasks/roadmap.yaml`, `tasks/TASK_TEMPLATE.md`, and an `ARCHITECTURE.md` template.

Then:

1. Write **ARCHITECTURE.md** (and optional domain context files under `docs/`).
2. Define sprints and tasks in **tasks/roadmap.yaml** using briefs under `tasks/`.
3. Add project-wide verification commands to **harness.yaml** when useful.

A good task brief is small, closed, and machine-checkable:

- **allowed_paths** — only directories/files the agent may edit.
- **Decisions** — do not reopen; state them in the brief.
- **Acceptance criteria** — observable outcomes.
- **verification** — shell commands the harness runs (not self-report).

Example frontmatter:

```yaml
---
id: S1-T1
stage: 1
model_class: worker
timeout_minutes: 30
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - src/example/**
verification:
  - npm test -- example
---
```

Commit harness, roadmap, and task files before the first automated run. Map stable context aliases in `harness.yaml` once architecture files exist. See [SPEC.md](SPEC.md) for validation rules and protected paths.

## Run a sprint

Replace `SPRINT` with the sprint key from `tasks/roadmap.yaml` (for example `sprint-01-example` after `harness init`, or `S1-tooling` in this repository).

```bash
harness validate SPRINT
harness graph SPRINT
harness run SPRINT --dry-run
harness doctor --full
harness run SPRINT
harness status SPRINT
harness resume SPRINT
```

`--dry-run` prints the stage plan without calling agents. A real `run` requires a clean integration checkout.

When a task is **BLOCKED**:

```bash
harness failure SPRINT
harness rerun-blocker SPRINT [--task TASK_ID] [--model-class escalation]
harness run-task TASK_ID --fresh-budget [--model-class escalation]
```

Then `harness resume SPRINT` continues from persisted state. For stage-barrier failures without re-invoking agents, `harness rerun-blocker SPRINT` retries merge and stage verification only.

Add work to a running sprint without stopping it:

```bash
harness add-task SPRINT path/to/brief.md [--stage auto|current|next]
```

Successful sprints can run configured **release** commands (see [SPEC.md](SPEC.md)). Retry a failed release with `harness release SPRINT`.

Operator workflows and edge cases: [HARNESS-OPERATOR.md](HARNESS-OPERATOR.md).

## Commands

Global options: `--root`, `--roadmap`, `--config` (see `harness --help`).

| Command | Purpose |
|--------|---------|
| `status [SPRINT]` | Sprint/task states and queue entries. |
| `graph SPRINT` | Stage and task DAG for a sprint. |
| `run SPRINT [--dry-run]` | Execute a sprint (or print the plan only). |
| `resume SPRINT` | Continue an in-progress or paused sprint. |
| `release SPRINT` | Run configured release commands after success. |
| `run-task TASK_ID [--model-class CLASS] [--fresh-budget]` | Run one task with optional fresh retry budget. |
| `failure SPRINT` | Show failure report and suggested next command. |
| `rerun-blocker SPRINT [--task TASK_ID] [--model-class CLASS]` | Focused recovery for a blocked task or stage barrier. |
| `retry TASK_ID` | Set a task to RETRY, reset attempt to 0, and clear review feedback. |
| `unblock TASK_ID` | Restore retry budget on a BLOCKED task. |
| `reset-task TASK_ID` | Remove unintegrated worktree/branch; task back to PENDING. |
| `verify TASK_ID` | Run mechanical verification for a task. |
| `review TASK_ID [--timeout-minutes N]` | Run independent reviewer for a task. |
| `inspect TASK_ID` | Task metadata, diff, and log paths. |
| `cleanup [--stale-worktrees] [--completed]` | Remove stale or completed harness worktrees. |
| `manifest SPRINT` | Active or latest run manifest for a sprint. |
| `usage` | Local agent invocation log summary. |
| `validate [SPRINT]` | Lint roadmap and task briefs. |
| `init [--force] [--public] [--no-github]` | Scaffold a new harness project (and GitHub repo by default). |
| `limits [--json]` | Codex, Cursor, and Claude usage limits (independent blocks). |
| `setup-machine` | Post-install machine setup invoked by install scripts. |
| `doctor [--full]` | Environment and config preflight. |
| `quota status` | Quota/capacity wait state. |
| `quota set-reset ISO8601` | Set expected quota reset time. |
| `quota clear-reset` | Clear configured quota reset time. |
| `plan-change propose SPRINT --task TASK_ID` | Start read-only plan-repair proposal. |
| `plan-change status` | Show open plan-change proposals. |
| `plan-change apply CR-ID --affected TASK_ID` | Apply an approved plan change. |
| `plan-change reject CR-ID` | Reject a proposal. |
| `add-task SPRINT BRIEF [--stage auto\|current\|next]` | Queue a new task during a run. |

## Usage and limits

```bash
harness usage
harness limits
harness limits --json
```

`harness limits` does not require `harness.yaml`. Each provider block is independent; partial failures do not hide other services. Details: [SPEC.md](SPEC.md).

Quota waiting and capacity waiting:

```bash
harness quota status
```

## Developing the harness itself

This repository is the harness; it is extended through its own roadmap in `tasks/roadmap.yaml` (sprints **S1**–**S7** and later). Read `AGENTS.md` before changing code.

Run checks locally:

```bash
python -m pytest -q tests/harness
python -m ruff check harness tests
python -m mypy harness
```

Or `scripts/check-harness.sh` (compile and pytest; ruff/mypy when those tools are installed; does not run `harness validate`).

After a successful harness sprint, **release** can reinstall the package for your user (see `harness.yaml` `release.commands`). Version history is in [CHANGELOG.md](CHANGELOG.md).
