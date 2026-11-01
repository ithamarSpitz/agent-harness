from __future__ import annotations

import json
import threading
from pathlib import Path

from .models import TaskRuntime, TaskStatus


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._data: dict[str, TaskRuntime] = {}
        self._meta: dict[str, object] = {}
        self._lock = threading.RLock()
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._meta = raw.get("meta", {})
            self._data = {
                task_id: TaskRuntime.from_json(value)
                for task_id, value in raw.get("tasks", {}).items()
            }

    def save(self) -> None:
        with self._lock:
            tmp = self.path.with_suffix(".tmp")
            payload = {
                "meta": self._meta,
                "tasks": {task_id: runtime.to_json() for task_id, runtime in sorted(self._data.items())},
            }
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            tmp.replace(self.path)

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
            self._data[task_id] = TaskRuntime()
            self.save()

    def meta(self) -> dict[str, object]:
        with self._lock:
            return dict(self._meta)

