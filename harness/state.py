from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .models import TaskRuntime, TaskStatus


class StateStore:
    def __init__(
        self,
        path: Path,
        *,
        replace_tries: int = 20,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.path = path
        self._replace_tries = max(1, replace_tries)
        self._sleep = sleep
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, TaskRuntime] = {}
        self._meta: dict[str, object] = {}
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            raw = self._read_raw()
            self._meta = raw.get("meta", {})
            self._data = {
                task_id: TaskRuntime.from_json(value)
                for task_id, value in raw.get("tasks", {}).items()
            }

    def _read_raw(self) -> dict[str, Any]:
        for attempt in range(self._replace_tries):
            try:
                data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
                return data
            except (PermissionError, json.JSONDecodeError):
                if attempt == self._replace_tries - 1:
                    raise
                self._sleep(0.1)
        raise AssertionError("unreachable")

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            payload = {
                "meta": self._meta,
                "tasks": {task_id: runtime.to_json() for task_id, runtime in sorted(self._data.items())},
            }
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            for attempt in range(self._replace_tries):
                try:
                    tmp.replace(self.path)
                    return
                except PermissionError:
                    if attempt == self._replace_tries - 1:
                        raise
                    self._sleep(0.1)

    def get(self, task_id: str) -> TaskRuntime:
        with self._lock:
            if task_id not in self._data:
                self._data[task_id] = TaskRuntime()
            return self._data[task_id]

    def set_status(self, task_id: str, status: TaskStatus, *, error: str | None = None) -> None:
        runtime = self.get(task_id)
        runtime.status = status
        runtime.last_error = error
        self.save()

    def all(self) -> dict[str, TaskRuntime]:
        with self._lock:
            return dict(self._data)

    def set_meta(self, key: str, value: object) -> None:
        self._meta[key] = value
        self.save()

    def get_meta(self, key: str, default: object = None) -> object:
        return self._meta.get(key, default)
    def reset_task(self, task_id: str) -> None:
        with self._lock:
            previous = self._data.get(task_id)
            added = bool(previous.added) if previous is not None else False
            brief_path = previous.brief_path if previous is not None else None
            self._data[task_id] = TaskRuntime(added=added, brief_path=brief_path)
            self.save()

    def meta(self) -> dict[str, object]:
        with self._lock:
            return dict(self._meta)

