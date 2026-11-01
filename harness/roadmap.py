from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .models import SprintSpec, TaskSpec


class RoadmapError(ValueError):
    pass


def _frontmatter(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}
    _, rest = text.split("---\n", 1)
    if "\n---\n" not in rest:
        raise RoadmapError(f"Unclosed YAML frontmatter in {path}")
    header, _ = rest.split("\n---\n", 1)
    return yaml.safe_load(header) or {}


class Roadmap:
    def __init__(self, root: Path, path: Path):
        self.root = root
        self.path = path
        self.raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        self.project = self.raw.get("project", root.name)
        self.sprints = self._parse_sprints()
        self._validate()

    def _parse_sprints(self) -> dict[str, SprintSpec]:
        result: dict[str, SprintSpec] = {}
        for sprint_id, raw_sprint in (self.raw.get("sprints") or {}).items():
            tasks: dict[str, TaskSpec] = {}
            for task_id, row in (raw_sprint.get("tasks") or {}).items():
                task_path = self.root / row["file"]
                if not task_path.exists():
                    raise RoadmapError(f"Task file does not exist: {task_path}")
                fm = _frontmatter(task_path)
                merged = {**fm, **row}
                tasks[task_id] = TaskSpec(
                    id=task_id,
                    file=Path(row["file"]),
                    sprint=sprint_id,
                    stage=int(merged.get("stage", 1)),
                    depends_on=list(merged.get("depends_on", [])),
                    model_class=str(merged.get("model_class", "worker")),
                    review_model_class=(
                        str(merged["review_model_class"])
                        if merged.get("review_model_class") is not None
                        else None
                    ),
                    max_attempts=(int(merged["max_attempts"]) if merged.get("max_attempts") is not None else None),
                    timeout_minutes=(int(merged["timeout_minutes"]) if merged.get("timeout_minutes") is not None else None),
                    review=bool(merged.get("review", True)),
                    allow_protected=bool(merged.get("allow_protected", False)),
                    allowed_paths=list(merged.get("allowed_paths", [])),
                    verification=list(merged.get("verification", [])),
                    context_refs=list(merged.get("context", [])),
                    environment=(str(merged["environment"]) if merged.get("environment") is not None else None),
                )
            stage_verification = {
                int(k): list(v or [])
                for k, v in (raw_sprint.get("stage_verification") or {}).items()
            }
            result[sprint_id] = SprintSpec(
                id=sprint_id,
                name=raw_sprint.get("name", sprint_id),
                tasks=tasks,
                stage_verification=stage_verification,
            )
        return result

    def _validate(self) -> None:
        task_locations: dict[str, str] = {}
        for sprint in self.sprints.values():
            for task_id in sprint.tasks:
                if task_id in task_locations:
                    raise RoadmapError(
                        f"Duplicate task id {task_id!r} appears in sprints "
                        f"{task_locations[task_id]!r} and {sprint.id!r}"
                    )
                task_locations[task_id] = sprint.id

        all_tasks = set(task_locations)
        if not all_tasks:
            raise RoadmapError("Roadmap has no tasks")
        for sprint in self.sprints.values():
            for task in sprint.tasks.values():
                missing = [dep for dep in task.depends_on if dep not in all_tasks]
                if missing:
                    raise RoadmapError(f"{task.id} depends on unknown tasks: {missing}")
        self._check_cycles()

    def _check_cycles(self) -> None:
        tasks = {tid: task for sprint in self.sprints.values() for tid, task in sprint.tasks.items()}
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(task_id: str) -> None:
            if task_id in visited:
                return
            if task_id in visiting:
                raise RoadmapError(f"Dependency cycle detected at {task_id}")
            visiting.add(task_id)
            for dep in tasks[task_id].depends_on:
                visit(dep)
            visiting.remove(task_id)
            visited.add(task_id)

        for task_id in tasks:
            visit(task_id)

    def sprint(self, sprint_id: str) -> SprintSpec:
        try:
            return self.sprints[sprint_id]
        except KeyError as exc:
            raise RoadmapError(f"Unknown sprint: {sprint_id}") from exc

    def task(self, task_id: str) -> TaskSpec:
        for sprint in self.sprints.values():
            if task_id in sprint.tasks:
                return sprint.tasks[task_id]
        raise RoadmapError(f"Unknown task: {task_id}")
