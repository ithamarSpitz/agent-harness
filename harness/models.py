from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    REVIEWING = "REVIEWING"
    VERIFIED = "VERIFIED"   # passed task checks, waiting for stage integration
    RETRY = "RETRY"
    DONE = "DONE"           # integrated and stage barrier passed
    BLOCKED = "BLOCKED"
    WAITING_FOR_QUOTA = "WAITING_FOR_QUOTA"
    WAITING_FOR_CAPACITY = "WAITING_FOR_CAPACITY"


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
    provider_attempts: dict[str, int] = field(default_factory=dict)

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
            "provider_attempts": dict(self.provider_attempts),
        }
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "TaskRuntime":
        return cls(
            status=TaskStatus(data.get("status", TaskStatus.PENDING.value)),
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
            provider_attempts={str(k): int(v) for k, v in (data.get("provider_attempts") or {}).items()},
        )
