from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .logging_utils import utc_now


@dataclass(slots=True)
class FailureRecord:
    sprint: str
    kind: str
    message: str
    stage: int | None = None
    task_ids: list[str] | None = None
    suggested_command: str | None = None
    created_at: str | None = None

    def to_json(self) -> dict[str, object]:
        data = asdict(self)
        data["created_at"] = self.created_at or utc_now()
        return data


class FailureStore:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, sprint: str) -> Path:
        return self.root / f"{sprint}.json"

    def record(self, record: FailureRecord) -> Path:
        path = self.path(record.sprint)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
        return path

    def clear(self, sprint: str) -> None:
        self.path(sprint).unlink(missing_ok=True)

    def get(self, sprint: str) -> dict[str, object] | None:
        path = self.path(sprint)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))


    @staticmethod
    def _task_ids(record: dict[str, object]) -> list[object]:
        value = record.get("task_ids")
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("Invalid failure record task_ids; expected a list")
        return value

    def resolve_task(self, sprint: str, task_id: str) -> None:
        record = self.get(sprint)
        if record is None or record.get("kind") != "TASK_BLOCKED":
            return
        task_ids = [str(x) for x in self._task_ids(record) if str(x) != task_id]
        if not task_ids:
            self.clear(sprint)
            return
        record["task_ids"] = task_ids
        record["message"] = "Remaining blocked tasks: " + ", ".join(task_ids)
        if len(task_ids) == 1:
            record["suggested_command"] = (
                f"python harness.py rerun-blocker {sprint} --model-class escalation"
            )
        else:
            record["suggested_command"] = (
                f"python harness.py rerun-blocker {sprint} --task <TASK_ID> --model-class escalation"
            )
        path = self.path(sprint)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)

    @staticmethod
    def render(record: dict[str, object]) -> str:
        lines = [
            f"STOPPED: {record.get('kind')}",
            f"Sprint: {record.get('sprint')}",
        ]
        if record.get("stage") is not None:
            lines.append(f"Stage: {record.get('stage')}")
        task_ids = FailureStore._task_ids(record)
        if task_ids:
            lines.append("Tasks: " + ", ".join(str(x) for x in task_ids))
        lines.append("Reason: " + str(record.get("message") or "unknown"))
        if record.get("suggested_command"):
            lines.append("Next: " + str(record["suggested_command"]))
        return "\n".join(lines)
