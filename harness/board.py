from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .headroom import Headroom, headroom_snapshot
from .models import SprintSpec, TaskSpec, TaskStatus
from .state import StateStore

_AGENT_STATUSES = frozenset(
    {
        TaskStatus.IMPLEMENTING,
        TaskStatus.CHECKING,
        TaskStatus.REVIEWING,
    }
)


@dataclass(frozen=True, slots=True)
class BoardTask:
    id: str
    status: str
    provider: str | None = None
    model: str | None = None
    attempt: int = 0
    max_attempts: int = 2
    phase_elapsed_seconds: int | None = None
    dependency_ids: list[str] = field(default_factory=list)
    added: bool = False
    routing_reason: str | None = None


@dataclass(frozen=True, slots=True)
class BoardStage:
    number: int
    merged: bool = False
    tasks: list[BoardTask] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class BoardEvent:
    time: datetime
    text: str


@dataclass(frozen=True, slots=True)
class BoardSnapshot:
    sprint_id: str
    sprint_title: str
    current_stage: int
    stage_count: int
    parallel_running: int | None = None
    parallel_max: int | None = None
    free_gb: float | None = None
    per_agent_gb: float | None = None
    stages: list[BoardStage] = field(default_factory=list)
    events: list[BoardEvent] = field(default_factory=list)
    headroom: Mapping[str, Sequence[Headroom]] | None = None


def _truncate(line: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(line) <= width:
        return line
    return line[:width]


def _format_elapsed(seconds: int) -> str:
    minutes_total = seconds // 60
    if minutes_total < 1:
        return ""
    if minutes_total < 60:
        return f"{minutes_total}m"
    hours = minutes_total // 60
    minutes = minutes_total % 60
    return f"{hours}h{minutes:02d}m"


def _format_gb(value: float) -> str:
    rounded = round(value, 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.1f}"


def _format_header(snapshot: BoardSnapshot) -> str:
    parts = [snapshot.sprint_id]
    if snapshot.sprint_title:
        parts.append(snapshot.sprint_title)
    parts.append(f"stage {snapshot.current_stage}/{snapshot.stage_count}")
    line = "   ".join(parts)
    if snapshot.parallel_running is not None and snapshot.parallel_max is not None:
        parallel = f"parallel {snapshot.parallel_running}/{snapshot.parallel_max}"
        if snapshot.free_gb is not None and snapshot.per_agent_gb is not None:
            parallel += (
                f" (free {_format_gb(snapshot.free_gb)} GB,"
                f" ~{_format_gb(snapshot.per_agent_gb)} GB/agent)"
            )
        line = f"{line}   {parallel}"
    return line


def _percent(entry: Headroom) -> str | None:
    if not entry.known or entry.used_percent is None:
        return None
    return str(round(entry.used_percent))


def _reset_clock(entry: Headroom) -> str:
    if not entry.known or entry.resets_at is None:
        return ""
    try:
        return datetime.fromtimestamp(entry.resets_at).strftime("%H:%M")
    except (OverflowError, OSError, ValueError):
        return ""


_WINDOW_LABELS = {"five_hour": "5h", "weekly": "wk"}


def _full_headroom_entry(provider: str, entries: Sequence[Headroom]) -> str:
    if provider == "cursor":
        values = [f"{_percent(e) or '?'}%" for e in entries]
        return f"{provider} " + "/".join(values)
    parts = [provider]
    for entry in entries:
        percent = _percent(entry)
        text = f"{percent}%" if percent is not None else "?%"
        label = (
            _WINDOW_LABELS.get(entry.window, entry.window)
            if provider in ("codex", "claude")
            else entry.window
        )
        parts.append(label)
        parts.append(text)
        if provider == "codex" and entry.window == "five_hour":
            clock = _reset_clock(entry)
            if clock:
                parts.append(f"({clock})")
    return " ".join(parts)


def _short_headroom_entry(provider: str, entries: Sequence[Headroom]) -> str:
    code = {"codex": "cx", "cursor": "cu", "claude": "cl"}.get(provider, provider[:2])
    return f"{code} " + "/".join(_percent(e) or "?" for e in entries)


def _format_headroom(snapshot: BoardSnapshot, width: int, *, unicode: bool) -> str | None:
    if not snapshot.headroom:
        return None
    items = [(name, entries) for name, entries in snapshot.headroom.items() if entries]
    if not items:
        return None
    separator = " · " if unicode else " | "
    full = separator.join(_full_headroom_entry(n, e) for n, e in items)
    if len(full) <= width:
        return full
    return separator.join(_short_headroom_entry(n, e) for n, e in items)


def _format_stage_header(stage: BoardStage, *, unicode: bool) -> str:
    line = f"Stage {stage.number}"
    if stage.merged:
        suffix = "  ✓ merged" if unicode else "  ok merged"
        line = f"{line}{suffix}"
    return line


def _is_agent_status(status_name: str) -> bool:
    try:
        return TaskStatus(status_name) in _AGENT_STATUSES
    except ValueError:
        return False


def _format_task_row(task: BoardTask, *, unicode: bool) -> str:
    parts = [task.id, task.status]
    if _is_agent_status(task.status) and task.provider and task.model:
        parts.extend([task.provider, task.model])
    parts.append(f"try {task.attempt}/{task.max_attempts}")
    if task.phase_elapsed_seconds is not None:
        elapsed = _format_elapsed(task.phase_elapsed_seconds)
        if elapsed:
            parts.append(elapsed)
    if task.dependency_ids:
        dep_arrow = "←" if unicode else "<-"
        parts.append(f"{dep_arrow} {', '.join(task.dependency_ids)}")
    if task.added:
        parts.append("(added)")
    return "  " + " ".join(parts)


def _format_routing_reason(task: BoardTask) -> str:
    reason = " ".join(task.routing_reason.split()) if task.routing_reason else ""
    return f"    {reason}" if reason else ""


def _aware(when: datetime) -> datetime:
    if when.tzinfo is None:
        return when.replace(tzinfo=timezone.utc)
    return when.astimezone(timezone.utc)


def _format_event(event: BoardEvent) -> str:
    when = _aware(event.time)
    return f"{when.hour:02d}:{when.minute:02d} {event.text}"


def render_board(snapshot: BoardSnapshot, width: int, *, unicode: bool = True) -> list[str]:
    lines: list[str] = [_format_header(snapshot)]
    headroom_line = _format_headroom(snapshot, width, unicode=unicode)
    if headroom_line:
        lines.append(headroom_line)
    for stage in snapshot.stages:
        lines.append(_format_stage_header(stage, unicode=unicode))
        for task in stage.tasks:
            lines.append(_format_task_row(task, unicode=unicode))
            reason_line = (
                _format_routing_reason(task) if _is_agent_status(task.status) else ""
            )
            if reason_line:
                lines.append(reason_line)
    if snapshot.events:
        newest = sorted(snapshot.events, key=lambda event: _aware(event.time), reverse=True)
        lines.append("")
        for event in newest[:5]:
            lines.append(_format_event(event))
    return [_truncate(line, width) for line in lines]


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _meta_bool(state: StateStore, key: str) -> bool:
    value = state.get_meta(key)
    if isinstance(value, bool):
        return value
    if value in (None, "", 0, "0", "false", "False"):
        return False
    return bool(value)


def _meta_int(state: StateStore, key: str, default: int | None = None) -> int | None:
    value = state.get_meta(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float, str, bytes, bytearray)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return default
    return default


def _meta_float(state: StateStore, key: str) -> float | None:
    value = state.get_meta(key)
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float, str, bytes, bytearray)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _meta_text(state: StateStore, key: str) -> str | None:
    value = state.get_meta(key)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _usage_path(state: StateStore) -> Path:
    return Path(state.path).parent / "usage.jsonl"


def _latest_routing_reasons(state: StateStore, sprint: SprintSpec) -> dict[str, str]:
    task_ids = set(sprint.tasks)
    reasons: dict[str, str] = {}
    try:
        lines = _usage_path(state).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return reasons
    for line in lines:
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict) or event.get("event_type") != "routing_choice":
            continue
        task_id = event.get("task_id")
        reason = event.get("routing_reason")
        if (
            isinstance(task_id, str)
            and task_id in task_ids
            and isinstance(reason, str)
            and reason.strip()
        ):
            reasons[task_id] = reason.strip()
    return reasons


def _task_stage(state: StateStore, sprint: SprintSpec, task: TaskSpec) -> int:
    override = _meta_int(state, f"tasks.{task.id}.stage")
    if override is not None:
        return override
    return task.stage


def _stage_numbers(sprint: SprintSpec, state: StateStore) -> list[int]:
    stages = {_task_stage(state, sprint, task) for task in sprint.tasks.values()}
    return sorted(stages) or [1]


def _current_stage(sprint: SprintSpec, state: StateStore, stages: list[int]) -> int:
    recorded = _meta_int(state, f"{sprint.id}.current_stage")
    if recorded is not None:
        return recorded
    open_stage: int | None = None
    for stage in stages:
        tasks = [task for task in sprint.tasks.values() if _task_stage(state, sprint, task) == stage]
        if not tasks:
            continue
        if any(state.get(task.id).status != TaskStatus.DONE for task in tasks):
            open_stage = stage
            break
    return open_stage if open_stage is not None else stages[-1]


def _stage_merged(state: StateStore, sprint: SprintSpec, stage_number: int, tasks: list[TaskSpec]) -> bool:
    if _meta_bool(state, f"{sprint.id}.stage.{stage_number}.merged"):
        return True
    return bool(tasks) and all(state.get(task.id).status == TaskStatus.DONE for task in tasks)


def _phase_elapsed_seconds(
    state: StateStore,
    sprint: SprintSpec,
    task: TaskSpec,
    runtime_status: TaskStatus,
    now: datetime,
) -> int | None:
    if runtime_status not in _AGENT_STATUSES:
        return None
    started = _parse_time(state.get_meta(f"{sprint.id}.{task.id}.phase_started_at"))
    if started is None:
        started = _parse_time(state.get(task.id).started_at)
    if started is None:
        return None
    elapsed = int((_aware(now) - _aware(started)).total_seconds())
    return max(0, elapsed)


def _board_task(
    state: StateStore,
    sprint: SprintSpec,
    task: TaskSpec,
    *,
    now: datetime,
    default_max_attempts: int,
    routing_reasons: Mapping[str, str],
) -> BoardTask:
    runtime = state.get(task.id)
    provider = _meta_text(state, f"{sprint.id}.{task.id}.board.provider")
    model = _meta_text(state, f"{sprint.id}.{task.id}.board.model")
    if provider is None:
        provider = runtime.waiting_provider
    if model is None and runtime.status in _AGENT_STATUSES:
        model = task.model_class
    routing_reason = _meta_text(state, f"{sprint.id}.{task.id}.board.routing_reason")
    if routing_reason is None:
        routing_reason = routing_reasons.get(task.id)
    max_attempts = task.max_attempts if task.max_attempts is not None else default_max_attempts
    return BoardTask(
        id=task.id,
        status=runtime.status.value,
        provider=provider,
        model=model,
        attempt=runtime.attempt,
        max_attempts=max_attempts,
        phase_elapsed_seconds=_phase_elapsed_seconds(state, sprint, task, runtime.status, now),
        dependency_ids=list(task.depends_on),
        added=runtime.added,
        routing_reason=routing_reason,
    )


def _live_headroom(
    source: Callable[[], Mapping[str, Sequence[Headroom]]] | None,
    order: Sequence[str] | None,
) -> dict[str, Sequence[Headroom]] | None:
    """Live headroom via headroom_snapshot (never the cache file); None on failure."""
    if source is None:
        return None
    try:
        raw = source()
    except Exception:
        return None
    if not raw:
        return None
    names = [name for name in (order or []) if name in raw]
    names += [name for name in raw if name not in names]
    return {name: list(raw[name]) for name in names}


def live_headroom_source() -> Mapping[str, Sequence[Headroom]]:
    """Read current headroom through the age-aware public API."""
    return headroom_snapshot()


def snapshot_from_state(
    state: StateStore,
    sprint: SprintSpec,
    now: datetime,
    *,
    events: Sequence[BoardEvent] | None = None,
    parallel_running: int | None = None,
    parallel_max: int | None = None,
    free_gb: float | None = None,
    per_agent_gb: float | None = None,
    default_max_attempts: int = 2,
    provider_order: Sequence[str] | None = None,
    headroom_source: Callable[[], Mapping[str, Sequence[Headroom]]] | None = None,
) -> BoardSnapshot:
    stages_sorted = _stage_numbers(sprint, state)
    current = _current_stage(sprint, state, stages_sorted)
    if parallel_running is None:
        parallel_running = _meta_int(state, f"{sprint.id}.board.parallel_running")
    if parallel_max is None:
        parallel_max = _meta_int(state, f"{sprint.id}.board.parallel_max")
    if free_gb is None:
        free_gb = _meta_float(state, f"{sprint.id}.board.free_gb")
    if per_agent_gb is None:
        per_agent_gb = _meta_float(state, f"{sprint.id}.board.per_agent_gb")
    if per_agent_gb is None:
        per_agent_gb = _meta_float(state, "resources.memory_per_agent_gb")
    routing_reasons = _latest_routing_reasons(state, sprint)

    board_stages: list[BoardStage] = []
    for stage_number in stages_sorted:
        tasks = [
            task for task in sprint.tasks.values() if _task_stage(state, sprint, task) == stage_number
        ]
        board_stages.append(
            BoardStage(
                number=stage_number,
                merged=_stage_merged(state, sprint, stage_number, tasks),
                tasks=[
                    _board_task(
                        state,
                        sprint,
                        task,
                        now=now,
                        default_max_attempts=default_max_attempts,
                        routing_reasons=routing_reasons,
                    )
                    for task in tasks
                ],
            )
        )

    headroom = _live_headroom(headroom_source, provider_order)
    return BoardSnapshot(
        sprint_id=sprint.id,
        sprint_title=sprint.name,
        current_stage=current,
        stage_count=len(stages_sorted),
        parallel_running=parallel_running,
        parallel_max=parallel_max,
        free_gb=free_gb,
        per_agent_gb=per_agent_gb,
        stages=board_stages,
        events=list(events) if events is not None else [],
        headroom=headroom,
    )
