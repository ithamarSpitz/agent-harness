from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .config import HarnessConfig
from .context import ContextResolver, ContextError
from .roadmap import Roadmap, RoadmapError, _frontmatter


@dataclass(slots=True)
class ValidationReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def render(self) -> str:
        lines: list[str] = []
        for item in self.errors:
            lines.append(f"ERROR {item}")
        for item in self.warnings:
            lines.append(f"WARN  {item}")
        if not lines:
            lines.append("OK    roadmap/tasks/context validation passed")
        return "\n".join(lines)


class ProjectValidator:
    def __init__(self, root: Path, roadmap: Roadmap, config: HarnessConfig):
        self.root = root.resolve()
        self.roadmap = roadmap
        self.config = config
        self.context = ContextResolver(self.root, config)

    def validate(self, sprint_id: str | None = None) -> ValidationReport:
        report = ValidationReport()
        selected_sprints = (
            [self.roadmap.sprint(sprint_id)] if sprint_id is not None else list(self.roadmap.sprints.values())
        )
        all_tasks = [task for sprint in selected_sprints for task in sprint.tasks.values()]
        seen: dict[str, str] = {}
        model_classes = set(self.config.cursor.models)

        if self.config.quota.policy not in {"wait", "stop"}:
            report.errors.append("quota.policy must be either 'wait' or 'stop'")
        if self.config.quota.probe_interval_minutes <= 0:
            report.errors.append("quota.probe_interval_minutes must be > 0")
        if self.config.quota.reset_grace_minutes < 0:
            report.errors.append("quota.reset_grace_minutes must be >= 0")
        if self.config.plan_repair.enabled and self.config.plan_repair.planner_model_class not in model_classes:
            report.warnings.append(
                "plan_repair.planner_model_class is not declared under cursor.models: "
                + repr(self.config.plan_repair.planner_model_class)
                + "; ordinary task execution can still run, but plan-change propose will be unavailable until configured"
            )

        if self.config.retry.sequence:
            for index, model_class in enumerate(self.config.retry.sequence, start=1):
                if model_class not in model_classes:
                    report.errors.append(
                        f"retry.sequence attempt {index} references undeclared model class {model_class!r}"
                    )

        for task in all_tasks:
            prior = seen.get(task.id)
            if prior is not None and prior != task.sprint:
                report.errors.append(f"duplicate task id {task.id!r} appears in sprints {prior!r} and {task.sprint!r}")
            else:
                seen[task.id] = task.sprint

            if task.stage < 1:
                report.errors.append(f"{task.id}: stage must be >= 1")
            if task.max_attempts is not None and task.max_attempts < 1:
                report.errors.append(f"{task.id}: max_attempts must be >= 1")
            if task.timeout_minutes is not None and task.timeout_minutes < 1:
                report.errors.append(f"{task.id}: timeout_minutes must be >= 1")
            if not task.allowed_paths:
                report.errors.append(f"{task.id}: allowed_paths must not be empty")
            if not task.verification:
                report.errors.append(f"{task.id}: at least one task verification command is required")
            if task.model_class not in model_classes:
                report.errors.append(
                    f"{task.id}: model_class {task.model_class!r} is not declared under cursor.models"
                )
            if task.review_model_class is not None and task.review_model_class not in model_classes:
                report.errors.append(
                    f"{task.id}: review_model_class {task.review_model_class!r} is not declared under cursor.models"
                )
            if (
                self.config.retry.sequence
                and task.max_attempts is not None
                and task.max_attempts > len(self.config.retry.sequence)
            ):
                report.errors.append(
                    f"{task.id}: max_attempts={task.max_attempts} exceeds retry.sequence length "
                    f"{len(self.config.retry.sequence)}"
                )
            if task.environment not in {None, "docker"}:
                report.errors.append(f"{task.id}: unsupported environment {task.environment!r}; expected docker or null")
            if task.environment == "docker" and not self.config.environment.docker_enabled:
                report.warnings.append(
                    f"{task.id}: environment=docker is declared but Docker task environments are currently disabled; "
                    "this is valid for a future sprint only if an earlier integrated task enables environment.docker_enabled before execution"
                )

            task_path = (self.root / task.file).resolve()
            try:
                task_path.relative_to(self.root)
            except ValueError:
                report.errors.append(f"{task.id}: task file escapes repository: {task.file}")
                continue

            try:
                fm = _frontmatter(task_path)
            except (OSError, RoadmapError) as exc:
                report.errors.append(f"{task.id}: cannot parse task frontmatter: {exc}")
                continue

            fm_id = fm.get("id")
            if fm_id is not None and str(fm_id) != task.id:
                report.errors.append(f"{task.id}: frontmatter id is {fm_id!r}, expected {task.id!r}")

            for pattern in task.allowed_paths:
                if not isinstance(pattern, str) or not pattern.strip():
                    report.errors.append(f"{task.id}: allowed_paths contains an empty/non-string pattern")
                    continue
                pathish = pattern.replace("\\", "/")
                if pathish.startswith("/") or pathish.startswith("../") or "/../" in f"/{pathish}":
                    report.errors.append(f"{task.id}: allowed path must stay repository-relative: {pattern}")

            try:
                self.context.resolve(task)
            except ContextError as exc:
                report.errors.append(f"{task.id}: {exc}")

            for dep_id in task.depends_on:
                try:
                    dep = self.roadmap.task(dep_id)
                except RoadmapError:
                    # Structural Roadmap validation normally catches this first.
                    continue
                if dep.sprint == task.sprint and dep.stage >= task.stage:
                    report.errors.append(
                        f"{task.id}: dependency {dep.id} is in stage {dep.stage}; same-sprint dependencies "
                        f"must be in an earlier stage than {task.stage}"
                    )

        for sprint in selected_sprints:
            task_stages = {task.stage for task in sprint.tasks.values()}
            unknown_stage_checks = sorted(set(sprint.stage_verification) - task_stages)
            for stage in unknown_stage_checks:
                report.warnings.append(
                    f"{sprint.id}: stage_verification is configured for stage {stage}, which has no tasks"
                )

        for alias, raw_path in self.config.context.aliases.items():
            if not alias.strip():
                report.errors.append("context.aliases contains an empty alias")
                continue
            try:
                self.context.resolve_ref(alias)
            except ContextError as exc:
                report.errors.append(str(exc))
            if raw_path == "AGENTS.md":
                report.warnings.append(
                    f"context alias {alias!r} points to AGENTS.md; AGENTS.md is already always loaded"
                )

        return report
