from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

HARNESS_AGENTS_START = "<!-- harness:start -->"
HARNESS_AGENTS_END = "<!-- harness:end -->"

HARNESS_AGENTS_BLOCK = f"""{HARNESS_AGENTS_START}
## Harness agent rules

- Work only on the current task brief; stay within `allowed_paths`.
- Do not commit; the harness owns commits and integration.
- When you finish, report: STATUS, CHANGED_FILES, TEST_RESULTS, OPEN_ISSUES.
{HARNESS_AGENTS_END}
"""

AGENTS_MD_NEW = f"""# Agent Operating Contract

{HARNESS_AGENTS_BLOCK}
"""

ARCHITECTURE_MD = """# Architecture

## Goal and users

Describe the product outcome and the people or systems it serves.

## Scope and non-goals

Define what the system includes and what it deliberately does not include.

## Stack

List the languages, frameworks, infrastructure, and key runtime constraints.

## Components (what each does and owns)

Describe each major component, its responsibility, and the data or behavior it owns.

## Data model

Document the important entities, relationships, storage, and lifecycle rules.

## Interfaces and contracts between components

Record APIs, events, file formats, and other boundaries components must preserve.

## Testing strategy (which commands prove what)

Map test layers and verification commands to the behavior they demonstrate.

## Decisions (dated, with the reason)

Record significant decisions with the date, chosen approach, and rationale.
"""

TASK_TEMPLATE_MD = """---
# Task identity and scheduling (required unless noted).
id: S?-T?
stage: 1
model_class: worker
# max_attempts: null uses execution.default_max_attempts
# timeout_minutes: null uses execution.default_timeout_minutes
review: true
allow_protected: false
depends_on: []
allowed_paths:
  - src/example/**
# Logical context aliases from harness.yaml, or repo-relative paths. Briefs should
# name the architecture sections they rely on when describing their required context.
context: []
# Set to docker only when environment.docker_enabled is true in harness.yaml.
environment: null
verification:
  - python -c "pass"
---

# Goal

One concrete outcome for this task.

# Scope

Implement exactly what belongs to this task.

# Out of scope

Nearby work that must not be pulled into this task.

# Acceptance criteria

1. Observable behavior one.
2. Observable behavior two.
"""

EXAMPLE_TASK_MD = """---
id: EX-T1
stage: 1
model_class: worker
review: false
allow_protected: false
depends_on: []
allowed_paths:
  - .gitignore
context:
  - architecture
environment: null
verification:
  - python -c "pass"
---

# Goal

Example task created by `harness init`; replace with real work.

# Scope

No code changes required for validation.

# Out of scope

Everything beyond proving the harness layout.

# Acceptance criteria

1. `harness validate` passes for the example sprint.
"""

HARNESS_YAML = """\
# Parallel agents, timeouts, retries and integration start guards.
execution:
  max_parallel_agents: 3
  default_max_attempts: 2
  default_timeout_minutes: 35
  base_ref: null
  keep_successful_worktrees: false
  stall_timeout_minutes: 12
  watchdog_poll_seconds: 10
  keep_awake_during_run: true

# One-time commands after each task worktree is created.
worktree:
  setup_commands: []
  setup_timeout_minutes: 10

# Graphify code map settings (optional `pip install graphifyy`).
code_map:
  command: graphify
  build_timeout_minutes: 5

# Cursor CLI routing; disable when using Codex only.
cursor:
  enabled: true
  command: agent
  output_format: stream-json
  trust_harness_worktrees: true
  models:
    worker: composer-2.5
    hard_worker: cursor-grok-4.6-high
    reviewer: cursor-grok-4.6-high

# Codex CLI routing; enable when ChatGPT-authenticated Codex is available.
codex:
  enabled: false
  command: codex
  reasoning_effort: high
  sandbox: workspace-write
  windows_implementation_sandbox: null
  approval_policy: never
  network_access: true
  review_model: gpt-5.6-sol
  planner_model: gpt-5.6-sol

# Provider order when multiple backends are enabled.
providers:
  priority:
    - codex
    - cursor

# Automatic model_class sequence for implementation attempts.
retry:
  sequence:
    - worker
    - hard_worker
    - hard_worker

# Mechanical checks after each task; edit global_task_commands for your stack.
verification:
  global_task_commands:
    - git diff --check
  stage_commands: []
  env: {}

# Files agents must not edit unless allow_protected is true.
paths:
  protected:
    - harness.yaml
    - tasks/**
    - AGENTS.md
    - ARCHITECTURE.md
  review_required: []

# Stable logical names for task context aliases.
context:
  aliases:
    architecture: ARCHITECTURE.md

# Per-task Docker/Compose isolation (opt in via task environment: docker).
environment:
  docker_enabled: false
  compose_command: docker compose
  compose_files:
    - docker-compose.yml
  project_name_prefix: harness
  services: []
  validate_compose: true
  up_before_agent: false
  teardown_on_success: true
  teardown_on_blocked: true
  remove_volumes: true
  command_timeout_minutes: 10
  env: {}

# Cursor included-usage exhaustion handling.
quota:
  policy: wait
  reset_at: null
  probe_interval_minutes: 60
  reset_grace_minutes: 5
  keep_awake: true
  teardown_environment_while_waiting: true

# Temporary provider capacity / overload handling.
capacity:
  policy: wait
  retry_interval_minutes: 5
  keep_awake: true
  teardown_environment_while_waiting: true

# Human-gated roadmap/architecture repair flow.
plan_repair:
  enabled: true
  planner_model_class: hard_worker
  timeout_minutes: 20

# Optional Playwright MCP UI check during independent review.
ui_review:
  enabled: false
  start_commands: []
  url: null
  ports: []
  playwright_mcp_package: "@playwright/mcp@0.0.84"

# Project-specific doctor requirements (empty in a fresh project).
doctor:
  required_commands: []
  required_env: []
  min_free_disk_gb: 1.0
  check_cursor_models: true
  minimum_versions: {}
"""

GITIGNORE_LINES = (".harness/",)

NEXT_STEPS = (
    "Next steps: edit harness.yaml verification commands, write briefs, "
    "run `harness doctor` and `harness validate`."
)


def _is_git_repo(root: Path) -> bool:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    return Path(result.stdout.strip()).resolve() == root.resolve()


def _has_origin(root: Path) -> bool:
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    return result.returncode == 0


def _github_command(root: Path, *, public: bool) -> list[str]:
    visibility = "--public" if public else "--private"
    return ["gh", "repo", "create", root.name, visibility, "--source", ".", "--remote", "origin"]


def _format_command(command: list[str]) -> str:
    return " ".join(command)


def _harness_git_commit_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Agent Harness")
    env.setdefault("GIT_AUTHOR_EMAIL", "harness@local")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    return env


def _setup_github(root: Path, *, public: bool, no_github: bool) -> str:
    if _has_origin(root):
        return "GitHub: skipped (origin exists)"
    if no_github:
        return "GitHub: skipped (--no-github)"

    command = _github_command(root, public=public)
    later = _format_command(command)
    gh = shutil.which("gh")
    if gh is None:
        return f"GitHub: WARNING: gh is unavailable; run `{later}` later."
    auth = subprocess.run(
        [gh, "auth", "status"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if auth.returncode != 0:
        return f"GitHub: WARNING: gh is not logged in; run `{later}` later."

    created = subprocess.run(
        [gh, *command[1:]],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if created.returncode != 0:
        return f"GitHub: WARNING: repository creation failed; run `{later}` later."
    return "GitHub: created origin remote"


def _roadmap_yaml(project_name: str) -> str:
    return f"""\
project: {project_name}
sprints:
  sprint-01-example:
    name: Example sprint
    tasks:
      EX-T1:
        file: tasks/sprint-01-example/01-example.md
        stage: 1
        depends_on: []
"""


def _display_path(root: Path, path: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix().replace("\\", "/")


def _write_template_file(
    root: Path,
    path: Path,
    content: str,
    *,
    force: bool,
    lines: list[str],
) -> None:
    display = _display_path(root, path)
    if path.exists() and not force:
        lines.append(f"{display}: skipped (exists)")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    lines.append(f"{display}: created")


def _update_agents_md(root: Path, path: Path, lines: list[str]) -> None:
    display = _display_path(root, path)
    if not path.exists():
        path.write_text(AGENTS_MD_NEW, encoding="utf-8")
        lines.append(f"{display}: created")
        return
    text = path.read_text(encoding="utf-8")
    if HARNESS_AGENTS_START in text and HARNESS_AGENTS_END in text:
        lines.append(f"{display}: skipped (exists)")
        return
    suffix = "" if text.endswith("\n") else "\n"
    path.write_text(text + suffix + "\n" + HARNESS_AGENTS_BLOCK + "\n", encoding="utf-8")
    lines.append(f"{display}: updated")


def _update_gitignore(root: Path, path: Path, lines: list[str]) -> None:
    display = _display_path(root, path)
    existing_lines: list[str] = []
    if path.exists():
        existing_lines = path.read_text(encoding="utf-8").splitlines()
    existing_set = set(existing_lines)
    to_append = [line for line in GITIGNORE_LINES if line not in existing_set]
    if not path.exists():
        path.write_text("\n".join(GITIGNORE_LINES) + "\n", encoding="utf-8")
        lines.append(f"{display}: created")
        return
    if not to_append:
        lines.append(f"{display}: skipped (exists)")
        return
    text = path.read_text(encoding="utf-8")
    if text and not text.endswith("\n"):
        text += "\n"
    text += "\n".join(to_append) + "\n"
    path.write_text(text, encoding="utf-8")
    lines.append(f"{display}: updated")


def run_init(
    root: Path,
    *,
    force: bool = False,
    public: bool = False,
    no_github: bool = False,
) -> int:
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    created_repo = not _is_git_repo(root)
    if created_repo:
        subprocess.run(
            ["git", "init", "-b", "main"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
        )
        git_line = "Git: initialized repository on main"
    else:
        git_line = "Git: existing repository"

    _write_template_file(root, root / "harness.yaml", HARNESS_YAML, force=force, lines=lines)
    _write_template_file(
        root,
        root / "ARCHITECTURE.md",
        ARCHITECTURE_MD,
        force=False,
        lines=lines,
    )
    _write_template_file(
        root,
        root / "tasks" / "roadmap.yaml",
        _roadmap_yaml(root.name),
        force=force,
        lines=lines,
    )
    _write_template_file(
        root,
        root / "tasks" / "TASK_TEMPLATE.md",
        TASK_TEMPLATE_MD,
        force=force,
        lines=lines,
    )
    _write_template_file(
        root,
        root / "tasks" / "sprint-01-example" / "01-example.md",
        EXAMPLE_TASK_MD,
        force=False,
        lines=lines,
    )
    _update_agents_md(root, root / "AGENTS.md", lines)
    _update_gitignore(root, root / ".gitignore", lines)

    github_line = _setup_github(root, public=public, no_github=no_github)
    if created_repo:
        subprocess.run(
            [
                "git",
                "add",
                "--",
                "harness.yaml",
                "ARCHITECTURE.md",
                "tasks/roadmap.yaml",
                "tasks/TASK_TEMPLATE.md",
                "tasks/sprint-01-example/01-example.md",
                "AGENTS.md",
                ".gitignore",
            ],
            cwd=root,
            check=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "Initial harness setup"],
            cwd=root,
            text=True,
            capture_output=True,
            check=True,
            env=_harness_git_commit_env(),
        )
        if _has_origin(root):
            subprocess.run(
                ["git", "push", "-u", "origin", "main"],
                cwd=root,
                text=True,
                capture_output=True,
                check=False,
            )
            commit_line = "First commit: created; push attempted"
        else:
            commit_line = "First commit: created; no origin to push"
    else:
        commit_line = "First commit: skipped (existing repository; files are ready to commit)"

    for line in lines:
        print(line)
    print(git_line)
    print(github_line)
    print(commit_line)
    print(NEXT_STEPS)
    return 0
