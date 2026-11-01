from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from .failure import FailureRecord, FailureStore
from .logging_utils import utc_now
from .models import TaskStatus
from .roadmap import Roadmap
from .provider_runner import ProviderAgentRunner
from .state import StateStore


def _hash_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _task_signature(task, root: Path | None = None) -> dict[str, Any]:
    data = {
        "file": task.file.as_posix(),
        "sprint": task.sprint,
        "stage": task.stage,
        "depends_on": list(task.depends_on),
        "model_class": task.model_class,
        "review_model_class": task.review_model_class,
        "max_attempts": task.max_attempts,
        "timeout_minutes": task.timeout_minutes,
        "review": task.review,
        "allow_protected": task.allow_protected,
        "allowed_paths": list(task.allowed_paths),
        "verification": list(task.verification),
        "context_refs": list(task.context_refs),
        "environment": task.environment,
    }
    if root is not None:
        data["task_file_sha256"] = _hash_file(root / task.file)
    return data


@dataclass(slots=True)
class ChangeRequest:
    id: str
    sprint: str
    task_id: str
    kind: str
    status: str
    created_at: str
    failure_summary: str
    planner_output: str
    plan_revision_before: int

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class PlanChangeManager:
    def __init__(
        self,
        root: Path,
        runtime_root: Path,
        state: StateStore,
        failures: FailureStore,
    ):
        self.root = root.resolve()
        self.runtime_root = runtime_root
        self.state = state
        self.failures = failures
        self.requests_root = runtime_root / "change_requests"
        self.revisions_root = runtime_root / "plan_revisions"
        self.requests_root.mkdir(parents=True, exist_ok=True)
        self.revisions_root.mkdir(parents=True, exist_ok=True)

    def revision(self) -> int:
        value = self.state.get_meta("plan.revision", 1)
        try:
            return max(1, int(cast(Any, value)))
        except (TypeError, ValueError):
            return 1

    def _snapshot_payload(self, roadmap: Roadmap, *, revision: int, change_id: str | None = None) -> dict[str, Any]:
        tasks = {
            task.id: _task_signature(task, self.root)
            for sprint in roadmap.sprints.values()
            for task in sprint.tasks.values()
        }
        return {
            "revision": revision,
            "recorded_at": utc_now(),
            "change_id": change_id,
            "architecture_sha256": _hash_file(self.root / "ARCHITECTURE.md"),
            "roadmap_sha256": _hash_file(roadmap.path),
            "agents_sha256": _hash_file(self.root / "AGENTS.md"),
            "tasks": tasks,
        }

    def snapshot_path(self, revision: int) -> Path:
        return self.revisions_root / f"{revision:04d}.json"

    def ensure_baseline(self, roadmap: Roadmap) -> Path:
        revision = self.revision()
        path = self.snapshot_path(revision)
        if not path.exists():
            path.write_text(
                json.dumps(self._snapshot_payload(roadmap, revision=revision), indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            self.state.set_meta("plan.revision", revision)
        return path

    def open_request_id(self) -> str | None:
        value = self.state.get_meta("plan.open_change_request")
        return str(value) if isinstance(value, str) and value else None

    def request_path(self, request_id: str) -> Path:
        return self.requests_root / f"{request_id}.json"

    def markdown_path(self, request_id: str) -> Path:
        return self.requests_root / f"{request_id}.md"

    def get(self, request_id: str) -> dict[str, Any]:
        path = self.request_path(request_id)
        if not path.exists():
            raise RuntimeError(f"Unknown change request: {request_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _next_id(self) -> str:
        existing = sorted(self.requests_root.glob("CR-*.json"))
        highest = 0
        for path in existing:
            try:
                highest = max(highest, int(path.stem.split("-")[-1]))
            except ValueError:
                pass
        return f"CR-{highest + 1:04d}"

    @staticmethod
    def _classify(output: str) -> str:
        for raw in output.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line.startswith("CHANGE_KIND:"):
                value = line.split(":", 1)[1].strip()
                if value in {"IMPLEMENTATION_ONLY", "TASK_ADDITION", "ARCHITECTURE_CHANGE", "UNKNOWN"}:
                    return value
            break
        return "UNKNOWN"

    def propose(
        self,
        *,
        roadmap: Roadmap,
        runner: ProviderAgentRunner,
        sprint_id: str,
        task_id: str,
        workspace: Path,
        failure_summary: str,
        timeout_minutes: int,
        model_class: str,
        env: dict[str, str] | None = None,
    ) -> ChangeRequest:
        if self.open_request_id():
            raise RuntimeError(f"A plan change is already open: {self.open_request_id()}")
        task = roadmap.task(task_id)
        if task.sprint != sprint_id:
            raise RuntimeError(f"Task {task_id} belongs to {task.sprint}, not {sprint_id}")
        result = runner.plan_change(
            task,
            workspace,
            failure_summary,
            timeout_minutes,
            model_class=model_class,
            env=env,
        )
        if result.quota_exhausted:
            raise RuntimeError("Agent provider usage quota exhausted while analyzing plan repair")
        if result.capacity_exhausted:
            raise RuntimeError("Agent provider capacity temporarily exhausted while analyzing plan repair")
        if not result.ok:
            raise RuntimeError(result.error or "Plan-repair analyst failed")
        kind = self._classify(result.output)
        request_id = self._next_id()
        status = "PROPOSED" if kind in {"TASK_ADDITION", "ARCHITECTURE_CHANGE", "UNKNOWN"} else "NO_PLAN_CHANGE"
        request = ChangeRequest(
            id=request_id,
            sprint=sprint_id,
            task_id=task_id,
            kind=kind,
            status=status,
            created_at=utc_now(),
            failure_summary=failure_summary,
            planner_output=result.output,
            plan_revision_before=self.revision(),
        )
        self.request_path(request_id).write_text(
            json.dumps(request.to_json(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.markdown_path(request_id).write_text(
            f"# {request_id} — {kind}\n\n"
            f"Sprint: `{sprint_id}`  \nTask: `{task_id}`  \nStatus: `{status}`\n\n"
            f"## Failure evidence\n\n```text\n{failure_summary}\n```\n\n"
            f"## Planner analysis\n\n{result.output.rstrip()}\n",
            encoding="utf-8",
        )
        if status == "PROPOSED":
            self.state.set_meta("plan.open_change_request", request_id)
            self.failures.record(
                FailureRecord(
                    sprint=sprint_id,
                    kind="PLAN_CHANGE_REQUIRED",
                    stage=task.stage,
                    task_ids=[task_id],
                    message=f"{kind} proposed in {self.markdown_path(request_id).relative_to(self.root)}",
                    suggested_command=(
                        f"Review {request_id}, edit/commit architecture/roadmap/task files, then: "
                        f"python harness.py plan-change apply {request_id}"
                    ),
                )
            )
        return request

    def reject(self, request_id: str) -> None:
        data = self.get(request_id)
        data["status"] = "REJECTED"
        data["resolved_at"] = utc_now()
        self.request_path(request_id).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if self.open_request_id() == request_id:
            self.state.set_meta("plan.open_change_request", None)
        self.failures.record(
            FailureRecord(
                sprint=str(data["sprint"]),
                kind="TASK_BLOCKED",
                task_ids=[str(data["task_id"])],
                message=(
                    f"Plan change {request_id} was rejected. Original failure remains: "
                    + str(data.get("failure_summary") or "blocked task")
                ),
                suggested_command=(
                    f"python harness.py rerun-blocker {data['sprint']} --model-class escalation"
                ),
            )
        )

    def apply(
        self,
        request_id: str,
        roadmap: Roadmap,
        *,
        affected_task_ids: list[str] | None = None,
        regenerate_contexts: bool = True,
    ) -> dict[str, Any]:
        data = self.get(request_id)
        if data.get("status") != "PROPOSED":
            raise RuntimeError(f"Change request {request_id} is {data.get('status')}, not PROPOSED")
        if self.open_request_id() != request_id:
            raise RuntimeError(f"Change request {request_id} is not the active plan-change gate")

        before_revision = int(data["plan_revision_before"])
        before_path = self.snapshot_path(before_revision)
        if not before_path.exists():
            raise RuntimeError(f"Missing baseline plan snapshot for revision {before_revision}")
        before = json.loads(before_path.read_text(encoding="utf-8"))

        if regenerate_contexts and (self.root / "scripts/generate-architecture-contexts.py").exists():
            proc = subprocess.run(
                ["python", "scripts/generate-architecture-contexts.py", "--check"],
                cwd=self.root,
                text=True, encoding="utf-8", errors="replace",
                capture_output=True,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    "Architecture context files are stale. Run "
                    "`python scripts/generate-architecture-contexts.py`, review/commit the generated files, "
                    f"then apply the change request again. Details: {proc.stdout}{proc.stderr}"
                )

        current_tasks = {
            task.id: _task_signature(task, self.root)
            for sprint in roadmap.sprints.values()
            for task in sprint.tasks.values()
        }
        previous_tasks = dict(before.get("tasks") or {})
        added = sorted(set(current_tasks) - set(previous_tasks))
        removed = sorted(set(previous_tasks) - set(current_tasks))
        changed = sorted(
            task_id for task_id in set(current_tasks) & set(previous_tasks)
            if current_tasks[task_id] != previous_tasks[task_id]
        )

        removed_started = [
            tid for tid in removed
            if self.state.get(tid).status not in {TaskStatus.PENDING}
            or self.state.get(tid).attempt > 0
            or self.state.get(tid).worktree
        ]
        if removed_started:
            raise RuntimeError(
                "Refusing to remove tasks that already have runtime history/worktrees: "
                + ", ".join(removed_started)
                + ". Supersede them with corrective tasks or keep the task and revise its dependencies/spec."
            )

        affected = sorted(set(affected_task_ids or []) | {str(data.get("task_id"))} | set(changed))
        unknown_affected = [tid for tid in affected if tid not in current_tasks]
        if unknown_affected:
            raise RuntimeError("Affected tasks are not present in the revised roadmap: " + ", ".join(unknown_affected))

        # A blocked/retry/waiting task named as affected gets a fresh budget.
        # DONE tasks remain historical DONE; they are revalidated separately.
        revalidate: list[str] = []
        for task_id in affected:
            runtime = self.state.get(task_id)
            if runtime.status == TaskStatus.DONE:
                revalidate.append(task_id)
            else:
                runtime.status = TaskStatus.PENDING
                runtime.attempt = 0
                runtime.last_error = f"Reset by applied plan change {request_id}"
                runtime.review_feedback = None
                runtime.waiting_phase = None
        self.state.save()

        pending_value = self.state.get_meta("plan.pending_revalidation", [])
        if not isinstance(pending_value, list):
            raise RuntimeError("Invalid plan.pending_revalidation state; expected a list")
        pending = {str(x) for x in pending_value}
        pending.update(revalidate)
        self.state.set_meta("plan.pending_revalidation", sorted(pending))

        new_revision = max(self.revision(), before_revision) + 1
        snapshot = self._snapshot_payload(roadmap, revision=new_revision, change_id=request_id)
        snapshot["diff"] = {
            "added_tasks": added,
            "removed_tasks": removed,
            "changed_tasks": changed,
            "affected_tasks": affected,
            "revalidation_tasks": sorted(revalidate),
        }
        self.snapshot_path(new_revision).write_text(
            json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        self.state.set_meta("plan.revision", new_revision)
        self.state.set_meta("plan.open_change_request", None)
        data["status"] = "APPLIED"
        data["applied_at"] = utc_now()
        data["plan_revision_after"] = new_revision
        data["diff"] = snapshot["diff"]
        self.request_path(request_id).write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.failures.clear(str(data["sprint"]))
        return snapshot["diff"]
