from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    IMPLEMENTING = "IMPLEMENTING"
    CHECKING = "CHECKING"
    REVIEWING = "REVIEWING"
    PASSED = "PASSED"       # passed task checks, waiting for stage integration
    MERGING = "MERGING"
    RETRY = "RETRY"
    DONE = "DONE"           # integrated and stage barrier passed
    BLOCKED = "BLOCKED"
    WAITING_QUOTA = "WAITING_QUOTA"
    WAITING_NETWORK = "WAITING_NETWORK"
    WAITING_PROVIDER_BUSY = "WAITING_PROVIDER_BUSY"
    WAITING_PROVIDER_ERROR = "WAITING_PROVIDER_ERROR"
    WAITING_TIMEOUT = "WAITING_TIMEOUT"


_LEGACY_TASK_STATUSES = {
    "RUNNING": TaskStatus.IMPLEMENTING,
    "VERIFYING": TaskStatus.CHECKING,
    "VERIFIED": TaskStatus.PASSED,
    "WAITING_FOR_QUOTA": TaskStatus.WAITING_QUOTA,
    "WAITING_FOR_CAPACITY": TaskStatus.WAITING_PROVIDER_ERROR,
}


def _task_status(value: object) -> TaskStatus:
    text = str(value)
    return _LEGACY_TASK_STATUSES.get(text) or TaskStatus(text)


@dataclass(slots=True)
class TaskSpec:
    id: str
    file: Path
    sprint: str
    stage: int
    depends_on: list[str] = field(default_factory=list)
    model_class: str = "worker"
    review_model_class: str | None = None
    max_attempts: int | None = None
    timeout_minutes: int | None = None
    review: bool = True
    allow_protected: bool = False
    allowed_paths: list[str] = field(default_factory=list)
    verification: list[str] = field(default_factory=list)
    context_refs: list[str] = field(default_factory=list)
    environment: str | None = None


@dataclass(slots=True)
class SprintSpec:
    id: str
    name: str
    tasks: dict[str, TaskSpec]
    stage_verification: dict[int, list[str]] = field(default_factory=dict)


@dataclass(slots=True)
class TaskRuntime:
    status: TaskStatus = TaskStatus.PENDING
    attempt: int = 0
    branch: str | None = None
    worktree: str | None = None
    base_ref: str | None = None
    commit: str | None = None
    last_error: str | None = None
    review_feedback: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    waiting_phase: str | None = None
    waiting_provider: str | None = None
    provider_attempts: dict[str, int] = field(default_factory=dict)
    stall_counts: dict[str, int] = field(default_factory=dict)
    added: bool = False
    brief_path: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = {
            "status": self.status.value,
            "attempt": self.attempt,
            "branch": self.branch,
            "worktree": self.worktree,
            "base_ref": self.base_ref,
            "commit": self.commit,
            "last_error": self.last_error,
            "review_feedback": self.review_feedback,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "waiting_phase": self.waiting_phase,
            "waiting_provider": self.waiting_provider,
            "provider_attempts": dict(self.provider_attempts),
            "stall_counts": dict(self.stall_counts),
            "added": self.added,
            "brief_path": self.brief_path,
        }
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TaskRuntime":
        return cls(
            status=_task_status(data.get("status", TaskStatus.PENDING.value)),
            attempt=int(data.get("attempt", 0)),
            branch=data.get("branch"),
            worktree=data.get("worktree"),
            base_ref=data.get("base_ref"),
            commit=data.get("commit"),
            last_error=data.get("last_error"),
            review_feedback=data.get("review_feedback"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            waiting_phase=data.get("waiting_phase"),
            waiting_provider=data.get("waiting_provider"),
            provider_attempts={str(k): int(v) for k, v in (data.get("provider_attempts") or {}).items()},
            stall_counts={str(k): int(v) for k, v in (data.get("stall_counts") or {}).items()},
            added=bool(data.get("added", False)),
            brief_path=data.get("brief_path"),
        )
