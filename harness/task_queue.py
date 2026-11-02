from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml

from .logging_utils import utc_now
from .models import SprintSpec, TaskSpec, TaskStatus
from .roadmap import Roadmap, RoadmapError, _frontmatter, task_spec_from_brief
from .release import sprint_is_complete
from .state import StateStore

if TYPE_CHECKING:
    from .scheduler import Scheduler

StageChoice = Literal["auto", "current", "next"]


@dataclass(slots=True)
class QueueSidecar:
    sprint: str
    stage_choice: StageChoice
    queued_at: str


class TaskQueueError(ValueError):
    pass


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)


def _as_int(value: object, default: int) -> int:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _git_commit_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_AUTHOR_NAME", "Agent Harness")
    env.setdefault("GIT_AUTHOR_EMAIL", "harness@local")
    env.setdefault("GIT_COMMITTER_NAME", env["GIT_AUTHOR_NAME"])
    env.setdefault("GIT_COMMITTER_EMAIL", env["GIT_AUTHOR_EMAIL"])
    return env


def _move_replace(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        dest.unlink()
    shutil.move(str(src), str(dest))


class TaskQueueManager:
    def __init__(self, repo_root: Path, runtime_root: Path, state: StateStore):
        self.repo_root = repo_root.resolve()
        self.runtime_root = runtime_root.resolve()
        self.state = state
        self.queue_dir = self.runtime_root / "queue"
        self.accepted_dir = self.queue_dir / "accepted"
        self.rejected_dir = self.queue_dir / "rejected"
        for directory in (self.queue_dir, self.accepted_dir, self.rejected_dir):
            directory.mkdir(parents=True, exist_ok=True)

    def _sidecar_path(self, task_id: str) -> Path:
        return self.queue_dir / f"{task_id}.json"

    def _brief_path(self, task_id: str) -> Path:
        return self.queue_dir / f"{task_id}.md"

    def _sprint_task_ids(self, sprint: SprintSpec) -> set[str]:
        ids = set(sprint.tasks)
        for path in self.queue_dir.glob("*.json"):
            if path.parent != self.queue_dir:
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if str(data.get("sprint")) == sprint.id:
                ids.add(path.stem)
        for task_id, runtime in self.state.all().items():
            if runtime.added:
                ids.add(task_id)
        return ids

    def validate_brief_for_queue(
        self,
        brief_path: Path,
        sprint_id: str,
        roadmap: Roadmap,
        *,
        exclude_task_id: str | None = None,
        require_incomplete_sprint: bool = True,
    ) -> tuple[str, dict[str, Any]]:
        brief_path = brief_path.resolve()
        if not brief_path.is_file():
            raise TaskQueueError(f"Task brief not found: {brief_path}")
        try:
            fm = _frontmatter(brief_path)
        except RoadmapError as exc:
            raise TaskQueueError(str(exc)) from exc
        except yaml.YAMLError as exc:
            raise TaskQueueError(f"Invalid task brief frontmatter: {exc}") from exc
        except OSError as exc:
            raise TaskQueueError(f"Cannot read task brief: {exc}") from exc

        if not isinstance(fm, dict) or not fm:
            raise TaskQueueError("Task brief frontmatter is missing or invalid")

        task_id = fm.get("id")
        if not task_id:
            raise TaskQueueError("Task brief frontmatter must include id")
        task_id = str(task_id)

        try:
            sprint = roadmap.sprint(sprint_id)
        except RoadmapError as exc:
            raise TaskQueueError(str(exc)) from exc

        self.merge_added_tasks_from_state(sprint)

        # Completeness is an add-time refusal only. Pickup must still place a
        # brief that was queued while the last stage was merging (tasks become
        # DONE before the post-merge drain) or during STAGE_BARRIER recovery.
        if require_incomplete_sprint and sprint_is_complete(self.state, sprint):
            raise TaskQueueError(f"Sprint {sprint_id} is already complete")

        known = self._sprint_task_ids(sprint)
        if exclude_task_id is not None:
            known.discard(exclude_task_id)
        if task_id in known:
            raise TaskQueueError(f"Task id {task_id!r} already exists in sprint {sprint_id}")

        raw_deps = fm.get("depends_on") or []
        if isinstance(raw_deps, str):
            raw_deps = [raw_deps]
        if not isinstance(raw_deps, list):
            raise TaskQueueError(f"Task {task_id}: depends_on must be a list")
        missing_deps = [str(dep) for dep in raw_deps if str(dep) not in self._sprint_task_ids(sprint)]
        if missing_deps:
            raise TaskQueueError(f"Unknown dependencies for {task_id}: {missing_deps}")

        allowed_paths = fm.get("allowed_paths") or []
        if isinstance(allowed_paths, str):
            allowed_paths = [allowed_paths]
        if not isinstance(allowed_paths, list) or not allowed_paths:
            raise TaskQueueError(f"Task {task_id}: allowed_paths must not be empty")

        return task_id, fm

    def enqueue(
        self,
        brief_path: Path,
        sprint_id: str,
        roadmap: Roadmap,
        *,
        stage_choice: str = "auto",
    ) -> Path:
        if stage_choice not in {"auto", "current", "next"}:
            raise TaskQueueError(
                f"Invalid stage choice {stage_choice!r}; expected auto, current, or next"
            )
        task_id, _fm = self.validate_brief_for_queue(brief_path, sprint_id, roadmap)

        sidecar = QueueSidecar(
            sprint=sprint_id,
            stage_choice=cast(StageChoice, stage_choice),
            queued_at=utc_now(),
        )
        brief_bytes = brief_path.read_bytes()
        _atomic_write_bytes(self._brief_path(task_id), brief_bytes)
        _atomic_write(
            self._sidecar_path(task_id),
            json.dumps(
                {
                    "sprint": sidecar.sprint,
                    "stage_choice": sidecar.stage_choice,
                    "queued_at": sidecar.queued_at,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return self._brief_path(task_id)

    def pending_entries(self, sprint_id: str | None = None) -> list[tuple[str, QueueSidecar, Path]]:
        entries: list[tuple[str, QueueSidecar, Path]] = []
        for sidecar_path in sorted(self.queue_dir.glob("*.json")):
            if sidecar_path.parent != self.queue_dir:
                continue
            task_id = sidecar_path.stem
            brief_path = self._brief_path(task_id)
            if not brief_path.exists():
                continue
            try:
                raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
                choice = str(raw.get("stage_choice", "auto"))
                if choice not in {"auto", "current", "next"}:
                    continue
                sidecar = QueueSidecar(
                    sprint=str(raw["sprint"]),
                    stage_choice=cast(StageChoice, choice),
                    queued_at=str(raw.get("queued_at", "")),
                )
            except (KeyError, json.JSONDecodeError, OSError):
                continue
            if sprint_id is not None and sidecar.sprint != sprint_id:
                continue
            entries.append((task_id, sidecar, brief_path))
        return entries

    def rejected_entries(self, sprint_id: str | None = None) -> list[tuple[str, str]]:
        results: list[tuple[str, str]] = []
        for reason_path in sorted(self.rejected_dir.glob("*.reason.txt")):
            task_id = reason_path.name[: -len(".reason.txt")]
            sidecar = self.rejected_dir / f"{task_id}.json"
            if sidecar.exists():
                try:
                    data = json.loads(sidecar.read_text(encoding="utf-8"))
                    if sprint_id is not None and str(data.get("sprint")) != sprint_id:
                        continue
                except (OSError, json.JSONDecodeError):
                    pass
            try:
                reason = reason_path.read_text(encoding="utf-8").strip()
            except OSError:
                reason = "(could not read reason)"
            results.append((task_id, reason))
        return results

    def _sprint_id_for_added_task(self, task_id: str) -> str | None:
        meta = self.state.get_meta(f"tasks.{task_id}.sprint")
        if meta:
            return str(meta)
        for directory in (self.accepted_dir, self.queue_dir):
            sidecar = directory / f"{task_id}.json"
            if not sidecar.is_file():
                continue
            try:
                data = json.loads(sidecar.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            sprint = data.get("sprint")
            if sprint:
                return str(sprint)
        return None

    def merge_added_tasks_from_state(self, sprint: SprintSpec) -> None:
        for task_id, runtime in self.state.all().items():
            if not runtime.added or task_id in sprint.tasks:
                continue
            owner = self._sprint_id_for_added_task(task_id)
            if owner is not None and owner != sprint.id:
                continue
            brief = runtime.brief_path
            if not brief:
                accepted = self.accepted_dir / f"{task_id}.md"
                if accepted.exists():
                    brief = str(accepted)
            if not brief:
                continue
            brief_path = Path(brief)
            if not brief_path.is_file():
                continue
            stage_value = self.state.get_meta(f"tasks.{task_id}.stage")
            stage = _as_int(stage_value, 1) if stage_value is not None else 1
            sprint.tasks[task_id] = task_spec_from_brief(
                task_id, sprint.id, brief_path, stage=stage
            )

    def resolve_task(self, roadmap: Roadmap, task_id: str) -> TaskSpec:
        try:
            return roadmap.task(task_id)
        except RoadmapError:
            pass
        runtime = self.state.all().get(task_id)
        if runtime is None or not runtime.added:
            raise RoadmapError(f"Unknown task: {task_id}")
        sprint_id = self._sprint_id_for_added_task(task_id)
        if sprint_id is None:
            raise RoadmapError(f"Unknown task: {task_id}")
        sprint = roadmap.sprint(sprint_id)
        self.merge_added_tasks_from_state(sprint)
        if task_id in sprint.tasks:
            return sprint.tasks[task_id]
        raise RoadmapError(f"Unknown task: {task_id}")

    def _dependencies_done(self, task: TaskSpec) -> bool:
        return all(self.state.get(dep).status == TaskStatus.DONE for dep in task.depends_on)

    def _brief_depends_on(self, brief_path: Path) -> list[str]:
        try:
            fm = _frontmatter(brief_path)
        except (OSError, RoadmapError, yaml.YAMLError):
            return []
        if not isinstance(fm, dict):
            return []
        raw = fm.get("depends_on") or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        return [str(dep) for dep in raw]

    def _order_pending_entries(
        self, entries: list[tuple[str, QueueSidecar, Path]]
    ) -> list[tuple[str, QueueSidecar, Path]]:
        if len(entries) <= 1:
            return list(entries)
        pending_ids = {task_id for task_id, _, _ in entries}
        by_id = {task_id: (sidecar, brief_path) for task_id, sidecar, brief_path in entries}
        pending_deps = {
            task_id: [dep for dep in self._brief_depends_on(brief_path) if dep in pending_ids]
            for task_id, _, brief_path in entries
        }
        remaining = [task_id for task_id, _, _ in entries]
        ordered: list[str] = []
        while remaining:
            ready = [
                task_id
                for task_id in remaining
                if all(dep not in remaining for dep in pending_deps.get(task_id, ()))
            ]
            if not ready:
                ordered.extend(remaining)
                break
            chosen = ready[0]
            ordered.append(chosen)
            remaining.remove(chosen)
        return [(task_id, by_id[task_id][0], by_id[task_id][1]) for task_id in ordered]

    def _same_sprint_dep_stages(
        self, sprint: SprintSpec, task: TaskSpec
    ) -> list[tuple[str, int]]:
        found: list[tuple[str, int]] = []
        for dep_id in task.depends_on:
            dep = sprint.tasks.get(dep_id)
            if dep is None or dep.sprint != sprint.id:
                continue
            found.append((dep_id, dep.stage))
        return found

    def _dependency_stage_order_ok(
        self, sprint: SprintSpec, task: TaskSpec, stage: int
    ) -> str | None:
        for dep_id, dep_stage in self._same_sprint_dep_stages(sprint, task):
            if dep_stage >= stage:
                return (
                    f"dependency {dep_id} is in stage {dep_stage}; same-sprint dependencies "
                    f"must be in an earlier stage than {stage}"
                )
        return None

    def _stage_occupants(
        self,
        sprint: SprintSpec,
        stage: int,
        extra: list[TaskSpec] | tuple[TaskSpec, ...] = (),
    ) -> list[TaskSpec]:
        occupants: dict[str, TaskSpec] = {}
        for existing in list(sprint.tasks.values()) + list(extra):
            if existing.stage == stage:
                occupants[existing.id] = existing
        return list(occupants.values())

    def _conflict_reason(
        self,
        task: TaskSpec,
        occupants: list[TaskSpec],
        scheduler: Scheduler,
    ) -> str | None:
        for existing in occupants:
            if scheduler._tasks_may_conflict(task, existing):
                return f"allowed_paths conflict with {existing.id}"
        return None

    def _next_stage(self, sprint: SprintSpec, current_stage: int) -> int:
        stages = sorted({t.stage for t in sprint.tasks.values()}) or [current_stage]
        if current_stage >= max(stages):
            return max(stages) + 1
        return current_stage + 1

    def _choose_stage(
        self,
        sprint: SprintSpec,
        task: TaskSpec,
        sidecar: QueueSidecar,
        *,
        current_stage: int,
        stage_tasks: list[TaskSpec],
        merge_in_progress: bool,
        scheduler: Scheduler,
    ) -> tuple[int | None, str | None]:
        choice = sidecar.stage_choice
        current_occupants = self._stage_occupants(sprint, current_stage, stage_tasks)

        def can_join_current() -> tuple[bool, str | None]:
            if merge_in_progress:
                return False, "stage merge already started"
            if not self._dependencies_done(task):
                missing = [
                    dep
                    for dep in task.depends_on
                    if self.state.get(dep).status != TaskStatus.DONE
                ]
                return False, f"dependencies not DONE: {', '.join(missing)}"
            conflict = self._conflict_reason(task, current_occupants, scheduler)
            if conflict:
                return False, conflict
            return True, None

        def place_next() -> tuple[int | None, str | None]:
            dest = self._next_stage(sprint, current_stage)
            dep_stages = self._same_sprint_dep_stages(sprint, task)
            later = [(dep_id, dep_stage) for dep_id, dep_stage in dep_stages if dep_stage > dest]
            if later:
                dep_id, dep_stage = later[0]
                return None, (
                    f"dependency {dep_id} is in stage {dep_stage}; same-sprint dependencies "
                    f"must be in an earlier stage than {dest}"
                )
            same_stage = [dep_stage for _, dep_stage in dep_stages if dep_stage == dest]
            if same_stage:
                dest = max(same_stage) + 1
            conflict = self._conflict_reason(
                task, self._stage_occupants(sprint, dest), scheduler
            )
            if conflict:
                return None, conflict
            return dest, None

        if choice == "current":
            ok, reason = can_join_current()
            if not ok:
                return None, reason or "cannot join current stage"
            return current_stage, None

        if choice == "next":
            return place_next()

        if choice == "auto":
            ok, _reason = can_join_current()
            if ok:
                return current_stage, None
            return place_next()

        return None, f"unsupported stage choice {choice!r}"

    def _accept(
        self,
        task_id: str,
        sidecar: QueueSidecar,
        brief_path: Path,
        sprint: SprintSpec,
        *,
        stage: int,
    ) -> TaskSpec:
        accepted_brief = self.accepted_dir / f"{task_id}.md"
        accepted_sidecar = self.accepted_dir / f"{task_id}.json"

        task = task_spec_from_brief(task_id, sprint.id, brief_path, stage=stage)
        sprint.tasks[task_id] = task
        runtime = self.state.get(task_id)
        runtime.added = True
        runtime.brief_path = str(accepted_brief.resolve())
        self.state.set_meta(f"tasks.{task_id}.stage", stage)
        self.state.set_meta(f"tasks.{task_id}.sprint", sprint.id)
        revision = max(1, _as_int(self.state.get_meta("plan.revision", 1), 1))
        self.state.set_meta("plan.revision", revision + 1)
        self.state.save()

        _move_replace(brief_path, accepted_brief)
        _move_replace(self._sidecar_path(task_id), accepted_sidecar)
        task.file = accepted_brief.resolve()
        return task

    def _reject(self, task_id: str, sidecar: QueueSidecar, brief_path: Path, reason: str) -> None:
        dest_brief = self.rejected_dir / f"{task_id}.md"
        dest_sidecar = self.rejected_dir / f"{task_id}.json"
        if brief_path.exists():
            _move_replace(brief_path, dest_brief)
        sidecar_path = self._sidecar_path(task_id)
        if sidecar_path.exists():
            _move_replace(sidecar_path, dest_sidecar)
        _atomic_write(self.rejected_dir / f"{task_id}.reason.txt", reason.strip() + "\n")

    def process_pending(
        self,
        sprint: SprintSpec,
        roadmap: Roadmap,
        *,
        current_stage: int,
        stage_tasks: list[TaskSpec],
        merge_in_progress: bool,
        scheduler: Scheduler,
    ) -> list[TaskSpec]:
        accepted: list[TaskSpec] = []
        for task_id, sidecar, brief_path in self._order_pending_entries(
            self.pending_entries(sprint.id)
        ):
            if task_id in sprint.tasks:
                self._reject(task_id, sidecar, brief_path, f"task id {task_id} already accepted")
                continue
            try:
                self.validate_brief_for_queue(
                    brief_path,
                    sprint.id,
                    roadmap,
                    exclude_task_id=task_id,
                    require_incomplete_sprint=False,
                )
            except TaskQueueError as exc:
                self._reject(task_id, sidecar, brief_path, str(exc))
                continue

            try:
                task_preview = task_spec_from_brief(
                    task_id, sprint.id, brief_path, stage=current_stage
                )
            except (RoadmapError, ValueError, TypeError) as exc:
                self._reject(task_id, sidecar, brief_path, str(exc))
                continue

            stage, reason = self._choose_stage(
                sprint,
                task_preview,
                sidecar,
                current_stage=current_stage,
                stage_tasks=stage_tasks,
                merge_in_progress=merge_in_progress,
                scheduler=scheduler,
            )
            if stage is None:
                self._reject(task_id, sidecar, brief_path, reason or "rejected at pickup")
                continue
            stage_order_reason = self._dependency_stage_order_ok(sprint, task_preview, stage)
            if stage_order_reason is not None:
                self._reject(task_id, sidecar, brief_path, stage_order_reason)
                continue
            task = self._accept(task_id, sidecar, brief_path, sprint, stage=stage)
            accepted.append(task)
        return accepted

    def _added_tasks_already_in_roadmap(
        self, sprint: SprintSpec, roadmap_path: Path, added: list[TaskSpec]
    ) -> bool:
        try:
            raw = yaml.safe_load(roadmap_path.read_text(encoding="utf-8")) or {}
        except OSError:
            return False
        tasks_raw = raw.get("sprints", {}).get(sprint.id, {}).get("tasks", {})
        if not isinstance(tasks_raw, dict):
            return False
        for task in added:
            entry = tasks_raw.get(task.id)
            if not isinstance(entry, dict):
                return False
            if entry.get("stage") != task.stage:
                return False
        return True

    def record_added_tasks_to_repo(
        self,
        sprint: SprintSpec,
        roadmap_path: Path,
        *,
        worktrees_clean_required: bool = True,
    ) -> bool:
        if self.state.get_meta(f"{sprint.id}.added_tasks_recorded"):
            return False
        added = [
            task
            for task in sprint.tasks.values()
            if self.state.get(task.id).added
        ]
        if not added:
            return False

        if self._added_tasks_already_in_roadmap(sprint, roadmap_path, added):
            proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.repo_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
            )
            if not proc.stdout.strip():
                self.state.set_meta(f"{sprint.id}.added_tasks_recorded", True)
                self.state.save()
            return False

        roadmap_tasks = [
            task for task in sprint.tasks.values() if not self.state.get(task.id).added
        ]
        if roadmap_tasks:
            target_dir = (self.repo_root / roadmap_tasks[0].file).parent
        elif added[0].file.is_absolute():
            target_dir = self.repo_root / "tasks" / sprint.id
        else:
            target_dir = (self.repo_root / added[0].file).parent

        if worktrees_clean_required:
            proc = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=self.repo_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
            )
            if proc.stdout.strip():
                raise RuntimeError(
                    "Integration checkout is dirty; cannot record added tasks to the repository"
                )

        target_dir.mkdir(parents=True, exist_ok=True)

        roadmap_backup = roadmap_path.read_text(encoding="utf-8")
        copied_briefs: list[Path] = []

        raw = yaml.safe_load(roadmap_backup) or {}
        sprint_raw = raw.setdefault("sprints", {}).setdefault(sprint.id, {})
        tasks_raw = sprint_raw.setdefault("tasks", {})

        rel_paths: list[str] = []
        for task in sorted(added, key=lambda t: t.id):
            src = Path(self.state.get(task.id).brief_path or task.file)
            if not src.is_file():
                src = self.accepted_dir / f"{task.id}.md"
            dest = target_dir / src.name
            shutil.copy2(src, dest)
            copied_briefs.append(dest)
            rel_file = dest.relative_to(self.repo_root).as_posix()
            rel_paths.append(rel_file)
            tasks_raw[task.id] = {
                "file": rel_file,
                "stage": task.stage,
                "depends_on": list(task.depends_on),
            }

        roadmap_path.write_text(
            yaml.safe_dump(raw, sort_keys=False, default_flow_style=False),
            encoding="utf-8",
        )

        commit_msg = f"harness: record added tasks for {sprint.id}"
        try:
            subprocess.run(
                ["git", "add", *rel_paths, str(roadmap_path.relative_to(self.repo_root))],
                cwd=self.repo_root,
                check=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            commit = subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=self.repo_root,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=_git_commit_env(),
                capture_output=True,
            )
            if commit.returncode != 0:
                raise RuntimeError(
                    commit.stderr.strip() or commit.stdout.strip() or "git commit failed"
                )
        except Exception:
            roadmap_path.write_text(roadmap_backup, encoding="utf-8")
            for path in copied_briefs:
                path.unlink(missing_ok=True)
            subprocess.run(
                ["git", "checkout", "--", str(roadmap_path.relative_to(self.repo_root))],
                cwd=self.repo_root,
                check=False,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            for path in copied_briefs:
                subprocess.run(
                    ["git", "clean", "-f", "--", str(path.relative_to(self.repo_root))],
                    cwd=self.repo_root,
                    check=False,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                )
            raise

        self.state.set_meta(f"{sprint.id}.added_tasks_recorded", True)
        self.state.save()
        return True
