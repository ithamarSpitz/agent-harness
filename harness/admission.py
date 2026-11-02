from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .agent_ledger import Ledger


@dataclass(frozen=True, slots=True)
class Admission:
    allowed: bool
    reason: str


def can_start(
    *,
    project: str,
    free_gb: float,
    estimate_gb: float,
    min_free_gb: float,
    max_parallel_agents: int,
    warmup_minutes: float = 3.0,
    ledger: Ledger,
    now: datetime,
) -> Admission:
    """Decide whether this harness may start one more agent right now.

    Pure: no I/O. Callers supply free memory, the machine-wide ledger snapshot,
    and the clock.
    """
    mine = _agent_count(ledger, project)

    if mine >= max_parallel_agents:
        return Admission(
            False,
            f"waiting: this project already runs {mine} agents",
        )

    available = free_gb - _reserved_gb(ledger, now, warmup_minutes) - min_free_gb

    if mine == 0:
        if available < estimate_gb:
            return Admission(
                True,
                (
                    f"starting first agent; {available:.1f} GB available, "
                    f"{estimate_gb:.1f} GB needed"
                ),
            )
        return Admission(True, "ok")

    if available < estimate_gb:
        return Admission(
            False,
            f"waiting: {available:.1f} GB available, {estimate_gb:.1f} GB needed",
        )

    for other_project, other_count in _waiting_projects(ledger).items():
        if other_project == project:
            continue
        if other_count < mine:
            label = _project_label(other_project)
            return Admission(
                False,
                (
                    f"waiting: {label} runs {other_count} agent"
                    f"{'' if other_count == 1 else 's'} and is waiting; "
                    f"this project runs {mine}"
                ),
            )

    return Admission(True, "ok")


def _agent_count(ledger: Ledger, project: str) -> int:
    return sum(1 for record in ledger.agents.values() if record.project == project)


def _waiting_projects(ledger: Ledger) -> dict[str, int]:
    waiting: dict[str, int] = {}
    for record in ledger.waiting.values():
        if record.ready_tasks <= 0:
            continue
        waiting[record.project] = _agent_count(ledger, record.project)
    return waiting


def _reserved_gb(ledger: Ledger, now: datetime, warmup_minutes: float) -> float:
    reserved = 0.0
    now_aware = _as_aware(now)
    for record in ledger.agents.values():
        started = _parse_started(record.started_at)
        age_minutes = (now_aware - started).total_seconds() / 60.0
        if age_minutes < warmup_minutes:
            reserved += record.estimate_gb
    return reserved


def _parse_started(started_at: str) -> datetime:
    return _as_aware(datetime.fromisoformat(started_at))


def _as_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _project_label(project: str) -> str:
    name = Path(project).name
    return name or project
