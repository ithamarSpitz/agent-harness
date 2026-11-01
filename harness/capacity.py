from __future__ import annotations

import time
from dataclasses import dataclass

from .config import HarnessConfig
from .models import TaskStatus
from .quota import KeepAwake
from .state import StateStore


@dataclass(slots=True)
class CapacityStatus:
    policy: str
    retry_interval_minutes: float
    waiting_tasks: list[str]


class CapacityManager:
    """Short-lived provider-capacity wait handling.

    Capacity is deliberately separate from account quota. A transient upstream
    `resource_exhausted` response should pause and retry without consuming task
    retry budget, but it must not claim that the user's monthly included usage
    is exhausted or sleep until a billing reset.
    """

    def __init__(self, config: HarnessConfig, state: StateStore):
        self.config = config
        self.state = state

    def waiting_tasks(self) -> list[str]:
        return sorted(
            task_id
            for task_id, runtime in self.state.all().items()
            if runtime.status == TaskStatus.WAITING_FOR_CAPACITY
        )

    def status(self) -> CapacityStatus:
        return CapacityStatus(
            policy=self.config.capacity.policy,
            retry_interval_minutes=self.config.capacity.retry_interval_minutes,
            waiting_tasks=self.waiting_tasks(),
        )

    def next_wait_seconds(self) -> float:
        return max(1.0, self.config.capacity.retry_interval_minutes * 60.0)

    def wait_once(self, *, sprint_id: str, sleep_fn=time.sleep) -> float:
        seconds = self.next_wait_seconds()
        self.state.set_meta(f"{sprint_id}.run_status", "WAITING_FOR_CAPACITY")
        with KeepAwake(self.config.capacity.keep_awake):
            remaining = seconds
            while remaining > 0:
                chunk = min(remaining, 60.0)
                sleep_fn(chunk)
                remaining -= chunk
        return seconds
