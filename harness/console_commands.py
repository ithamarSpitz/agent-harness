"""Console command registry and command handlers."""

from __future__ import annotations

import io
import json
import shlex
import subprocess
from contextlib import redirect_stdout
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .board import snapshot_from_state
from .codex_runner import CodexAgentRunner
from .limits import run_limits
from .models import TaskSpec, TaskStatus
from .runner import CursorAgentRunner

Handler = Callable[["ConsoleContext", list[str]], list[str]]
GitDiffStat = Callable[[Path, str], list[str]]


class QuitSignal(Exception):
    """Raised by the quit command; callers should leave the console."""


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    args_help: str
    help: str
    changes_state: bool
    handler: Handler


@dataclass(slots=True)
class ConsoleContext:
    """What console command handlers need from the live run."""

    runtime: Any
    sprint_id: str
    confirm: Callable[[str], bool]
    call_cli: Callable[..., list[str]]
    now: datetime | None = None
    git_diff_stat: GitDiffStat | None = None


def capture_cli(func: Callable[..., Any], *args: Any, **kwargs: Any) -> list[str]:
    """Run a CLI-style function that prints, returning stdout as lines."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        func(*args, **kwargs)
    return buffer.getvalue().splitlines()


def _format_elapsed(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    minutes_total = seconds // 60
    if minutes_total < 1:
        return f"{seconds}s"
    if minutes_total < 60:
        return f"{minutes_total}m"
    hours = minutes_total // 60
    minutes = minutes_total % 60
    return f"{hours}h{minutes:02d}m"


def _sprint_tasks(context: ConsoleContext) -> dict[str, TaskSpec]:
    sprint = context.runtime.roadmap.sprint(context.sprint_id)
    return dict(sprint.tasks)


def _resolve_known_task(context: ConsoleContext, task_id: str) -> TaskSpec | None:
    tasks = _sprint_tasks(context)
    if task_id in tasks:
        return tasks[task_id]
    scheduler = getattr(context.runtime, "scheduler", None)
    task_queue = getattr(scheduler, "task_queue", None) if scheduler is not None else None
    roadmap = getattr(context.runtime, "roadmap", None)
    if task_queue is not None and roadmap is not None and hasattr(task_queue, "resolve_task"):
        try:
            return task_queue.resolve_task(roadmap, task_id)
        except Exception:
            return None
    return None


def _unknown_task(task_id: str) -> list[str]:
    return [f"Unknown task: {task_id}"]


def _usage(command: Command) -> list[str]:
    args = f" {command.args_help}" if command.args_help else ""
    return [f"Usage: {command.name}{args}"]


def _default_max_attempts(context: ConsoleContext) -> int:
    config = getattr(context.runtime, "config", None)
    execution = getattr(config, "execution", None)
    value = getattr(execution, "default_max_attempts", None)
    try:
        return int(value) if value is not None else 2
    except (TypeError, ValueError):
        return 2


def _board_task_for(context: ConsoleContext, task: TaskSpec):
    sprint = context.runtime.roadmap.sprint(context.sprint_id)
    now = context.now or datetime.now(timezone.utc)
    snapshot = snapshot_from_state(
        context.runtime.state,
        sprint,
        now,
        default_max_attempts=_default_max_attempts(context),
    )
    for stage in snapshot.stages:
        for board_task in stage.tasks:
            if board_task.id == task.id:
                return board_task
    return None


def _logs_dir(context: ConsoleContext) -> Path:
    root = Path(context.runtime.root)
    return root / ".harness" / "logs"


def _agent_log_paths(context: ConsoleContext, task_id: str) -> list[Path]:
    log_dir = _logs_dir(context)
    if not log_dir.is_dir():
        return []
    patterns = (
        f"{task_id}-attempt-*.log",
        f"{task_id}-review.log",
        f"{task_id}-review-codex.log",
        f"{task_id}-plan-repair.log",
        f"{task_id}-plan-repair-codex.log",
    )
    paths: list[Path] = []
    for pattern in patterns:
        paths.extend(log_dir.glob(pattern))
    return [path for path in paths if path.is_file()]


def _latest_agent_log(context: ConsoleContext, task_id: str) -> Path | None:
    paths = _agent_log_paths(context, task_id)
    if not paths:
        return None
    return max(paths, key=lambda path: path.stat().st_mtime)


def _last_agent_message(raw: str) -> str:
    cursor_answer, _, _, _, _ = CursorAgentRunner._parse_output(raw)
    codex_answer, _, _, _ = CodexAgentRunner._parse_events(raw)

    saw_cursor_json = False
    for line in raw.splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") in {"assistant", "result"}:
            saw_cursor_json = True
            break

    saw_codex_message = bool(codex_answer)
    if saw_cursor_json and cursor_answer.strip():
        return cursor_answer.strip()
    if saw_codex_message:
        return codex_answer.strip()
    if cursor_answer.strip() and cursor_answer.strip() != raw.strip():
        return cursor_answer.strip()
    return raw.strip()


def _git_diff_stat(worktree: Path, base_ref: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--stat", base_ref],
        cwd=worktree,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    output = (result.stdout or "").strip()
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "git diff failed").strip()
        return [err.splitlines()[0] if err else "git diff failed"]
    if not output:
        return ["(no changes)"]
    return output.splitlines()


def cmd_help(context: ConsoleContext, args: list[str]) -> list[str]:
    del context, args
    lines: list[str] = []
    for command in COMMANDS:
        suffix = f" {command.args_help}" if command.args_help else ""
        left = f"{command.name}{suffix}"
        lines.append(f"{left:<24} {command.help}")
    return lines


def cmd_status(context: ConsoleContext, args: list[str]) -> list[str]:
    del context, args
    return []


def cmd_task(context: ConsoleContext, args: list[str]) -> list[str]:
    if len(args) != 1:
        return _usage(COMMAND_BY_NAME["task"])
    task_id = args[0]
    task = _resolve_known_task(context, task_id)
    if task is None:
        return _unknown_task(task_id)
    board_task = _board_task_for(context, task)
    runtime = context.runtime.state.get(task_id)
    status = board_task.status if board_task is not None else runtime.status.value
    provider = board_task.provider if board_task is not None else runtime.waiting_provider
    model = board_task.model if board_task is not None else None
    attempt = board_task.attempt if board_task is not None else runtime.attempt
    elapsed = board_task.phase_elapsed_seconds if board_task is not None else None
    added = board_task.added if board_task is not None else runtime.added
    deps = board_task.dependency_ids if board_task is not None else list(task.depends_on)
    return [
        f"task: {task_id}",
        f"status: {status}",
        f"provider: {provider or '-'}",
        f"model: {model or '-'}",
        f"attempt: {attempt}",
        f"elapsed: {_format_elapsed(elapsed)}",
        f"allowed_paths: {', '.join(task.allowed_paths) if task.allowed_paths else '-'}",
        f"depends_on: {', '.join(deps) if deps else '-'}",
        f"added: {'yes' if added else 'no'}",
    ]


def cmd_why(context: ConsoleContext, args: list[str]) -> list[str]:
    if len(args) != 1:
        return _usage(COMMAND_BY_NAME["why"])
    task_id = args[0]
    if _resolve_known_task(context, task_id) is None:
        return _unknown_task(task_id)
    runtime = context.runtime.state.get(task_id)
    text = runtime.review_feedback or runtime.last_error
    if not text:
        failures = getattr(context.runtime, "failures", None)
        record = failures.get(context.sprint_id) if failures is not None else None
        if isinstance(record, dict):
            task_ids = record.get("task_ids") or []
            if not task_ids or task_id in {str(item) for item in task_ids}:
                text = str(record.get("message") or "") or None
    if not text:
        return [f"No failure recorded for {task_id}."]
    return str(text).splitlines() or [str(text)]


def cmd_log(context: ConsoleContext, args: list[str]) -> list[str]:
    if len(args) != 1:
        return _usage(COMMAND_BY_NAME["log"])
    task_id = args[0]
    if _resolve_known_task(context, task_id) is None:
        return _unknown_task(task_id)
    path = _latest_agent_log(context, task_id)
    if path is None:
        return [f"No agent log for {task_id}."]
    message = _last_agent_message(path.read_text(encoding="utf-8", errors="replace"))
    if not message:
        return [f"No agent message in {path.name}."]
    return message.splitlines()


def cmd_diff(context: ConsoleContext, args: list[str]) -> list[str]:
    if len(args) != 1:
        return _usage(COMMAND_BY_NAME["diff"])
    task_id = args[0]
    if _resolve_known_task(context, task_id) is None:
        return _unknown_task(task_id)
    runtime = context.runtime.state.get(task_id)
    if not runtime.worktree or not runtime.base_ref:
        return [f"No worktree/base for {task_id}."]
    worktree = Path(runtime.worktree)
    if not worktree.is_dir():
        return [f"Worktree missing for {task_id}."]
    if context.git_diff_stat is not None:
        return list(context.git_diff_stat(worktree, runtime.base_ref))
    return _git_diff_stat(worktree, runtime.base_ref)


def cmd_usage(context: ConsoleContext, args: list[str]) -> list[str]:
    del args
    return context.call_cli(run_limits)


def _cancelled() -> list[str]:
    return ["Cancelled."]


def _parse_add_args(args: list[str]) -> tuple[str, str] | list[str]:
    """Return (brief, stage) or usage/error lines."""
    command = COMMAND_BY_NAME["add"]
    if not args:
        return _usage(command)
    brief = args[0]
    stage = "auto"
    index = 1
    while index < len(args):
        token = args[index]
        if token == "--stage":
            if index + 1 >= len(args):
                return ["Missing value for --stage (expected auto, current, or next)."]
            stage = args[index + 1]
            if stage not in {"auto", "current", "next"}:
                return [f"Invalid --stage value {stage!r}; expected auto, current, or next."]
            index += 2
            continue
        return _usage(command)
    return brief, stage


def cmd_add(context: ConsoleContext, args: list[str]) -> list[str]:
    from .cli import cmd_add_task

    parsed = _parse_add_args(args)
    if isinstance(parsed, list):
        return parsed
    brief, stage = parsed
    root = Path(context.runtime.root)
    path = Path(brief)
    brief_path = path.resolve() if path.is_absolute() else (root / path).resolve()
    prompt = f"Add task {brief_path.as_posix()} (stage={stage})? [y/N]"
    if not context.confirm(prompt):
        return _cancelled()
    return context.call_cli(
        cmd_add_task,
        context.runtime,
        context.sprint_id,
        brief_path,
        stage_choice=stage,
    )


def cmd_retry(context: ConsoleContext, args: list[str]) -> list[str]:
    from .cli import cmd_rerun_blocker

    if len(args) != 1:
        return _usage(COMMAND_BY_NAME["retry"])
    task_id = args[0]
    if _resolve_known_task(context, task_id) is None:
        return _unknown_task(task_id)
    runtime = context.runtime.state.get(task_id)
    status = runtime.status
    if status != TaskStatus.BLOCKED:
        return [f"{task_id} is {status.value}, not BLOCKED; cannot retry."]
    if not context.confirm(f"Retry blocked task {task_id}? [y/N]"):
        return _cancelled()
    return context.call_cli(
        cmd_rerun_blocker,
        context.runtime,
        context.sprint_id,
        None,
        task_id,
    )


def cmd_stop(context: ConsoleContext, args: list[str]) -> list[str]:
    if args:
        return _usage(COMMAND_BY_NAME["stop"])
    if not context.confirm(f"Stop sprint {context.sprint_id}? [y/N]"):
        return _cancelled()
    now = context.now or datetime.now(timezone.utc)
    harness_dir = Path(context.runtime.root) / ".harness"
    harness_dir.mkdir(parents=True, exist_ok=True)
    path = harness_dir / "stop-request"
    payload = {"sprint": context.sprint_id, "time": now.isoformat()}
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return [f"Stop requested for {context.sprint_id}."]


def cmd_quit(context: ConsoleContext, args: list[str]) -> list[str]:
    del context
    if args:
        return _usage(COMMAND_BY_NAME["quit"])
    raise QuitSignal()


COMMANDS: list[Command] = [
    Command("help", "", "List console commands", False, cmd_help),
    Command("status", "", "Board already shows status", False, cmd_status),
    Command("task", "<id>", "Show task status and details", False, cmd_task),
    Command("why", "<id>", "Show last reviewer findings or error", False, cmd_why),
    Command("log", "<id>", "Show last agent message from latest call", False, cmd_log),
    Command("diff", "<id>", "Show git diff --stat for the task worktree", False, cmd_diff),
    Command("usage", "", "Show provider usage limits (harness limits)", False, cmd_usage),
    Command(
        "add",
        "<brief> [--stage auto|current|next]",
        "Queue a task brief (harness add-task)",
        True,
        cmd_add,
    ),
    Command("retry", "<id>", "Rerun a blocked task (harness rerun-blocker)", True, cmd_retry),
    Command("stop", "", "Request the run to stop after current work", True, cmd_stop),
    Command("quit", "", "Leave the console", False, cmd_quit),
]

COMMAND_BY_NAME: dict[str, Command] = {command.name: command for command in COMMANDS}


def run_command(line: str, context: ConsoleContext) -> list[str]:
    """Parse a console line, dispatch the command, and return output lines."""
    text = line.strip()
    if not text:
        return []
    try:
        parts = shlex.split(text, posix=True)
    except ValueError as exc:
        return [f"Could not parse command: {exc}"]
    if not parts:
        return []
    name, *args = parts
    command = COMMAND_BY_NAME.get(name)
    if command is None:
        return [f"Unknown command: {name}. Type 'help' for a list."]
    return command.handler(context, args)
