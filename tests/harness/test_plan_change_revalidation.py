from __future__ import annotations

from pathlib import Path

from harness.failure import FailureStore
from harness.models import TaskStatus
from harness.plan_change import PlanChangeManager
from harness.roadmap import Roadmap
from harness.runner import AgentResult
from harness.state import StateStore


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class PlanRunner:
    def plan_change(self, *args, **kwargs):
        return AgentResult(True, "CHANGE_KIND: TASK_ADDITION\n")


def write_brief(path: Path, title: str) -> None:
    write(path, f"---\nreview: false\n---\n# {title}\n")


def make_roadmap(root: Path) -> Roadmap:
    write(
        root / "tasks/roadmap.yaml",
        """project: revalidation-test
sprints:
  s0:
    tasks:
      S0-T1: {file: tasks/S0-T1.md, stage: 1}
  s1:
    tasks:
      S1-T1: {file: tasks/S1-T1.md, stage: 1}
      S1-T2: {file: tasks/S1-T2.md, stage: 2}
  s2:
    tasks:
      S2-T1: {file: tasks/S2-T1.md, stage: 1}
  s6:
    tasks:
      S6-T1: {file: tasks/S6-T1.md, stage: 1}
""",
    )
    return Roadmap(root, root / "tasks/roadmap.yaml")


def setup_manager(root: Path) -> tuple[PlanChangeManager, StateStore]:
    state = StateStore(root / ".harness/state.json")
    manager = PlanChangeManager(
        root,
        root / ".harness",
        state,
        FailureStore(root / ".harness/failures"),
    )
    return manager, state


def propose(manager: PlanChangeManager, roadmap: Roadmap):
    return manager.propose(
        roadmap=roadmap,
        runner=PlanRunner(),
        sprint_id="s6",
        task_id="S6-T1",
        workspace=roadmap.root,
        failure_summary="newest sprint needs a plan repair",
        timeout_minutes=1,
        model_class="escalation",
    )


def test_apply_revalidates_only_task_changed_since_proposal(tmp_path: Path):
    task_ids = ["S0-T1", "S1-T1", "S1-T2", "S2-T1", "S6-T1"]
    for task_id in task_ids:
        write_brief(tmp_path / f"tasks/{task_id}.md", f"initial {task_id}")
    roadmap = make_roadmap(tmp_path)
    manager, state = setup_manager(tmp_path)

    # A revision snapshot can predate the proposal by several completed sprints.
    manager.ensure_baseline(roadmap)
    for task_id in task_ids[:-1]:
        write_brief(tmp_path / f"tasks/{task_id}.md", f"completed {task_id}")
    proposal_roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    for task_id in task_ids:
        state.get(task_id).status = TaskStatus.DONE
    state.save()

    request = propose(manager, proposal_roadmap)
    write_brief(tmp_path / "tasks/S6-T1.md", "revised S6-T1")
    revised = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    diff = manager.apply(request.id, revised, regenerate_contexts=False)

    assert diff["changed_tasks"] == ["S6-T1"]
    assert diff["revalidation_tasks"] == ["S6-T1"]
    assert state.get_meta("plan.pending_revalidation") == ["S6-T1"]


def test_missing_proposal_signature_is_logged_and_not_revalidated(
    tmp_path: Path, capsys
):
    write_brief(tmp_path / "tasks/S6-T1.md", "unchanged S6-T1")
    roadmap = make_roadmap_with_only_newest(tmp_path)
    manager, state = setup_manager(tmp_path)
    state.get("S6-T1").status = TaskStatus.DONE
    state.save()
    request = propose(manager, roadmap)

    baseline_path = manager.snapshot_path(request.plan_revision_before)
    baseline = baseline_path.read_text(encoding="utf-8")
    import json

    payload = json.loads(baseline)
    del payload["tasks"]["S6-T1"]
    baseline_path.write_text(json.dumps(payload), encoding="utf-8")

    diff = manager.apply(request.id, roadmap, regenerate_contexts=False)

    assert diff["changed_tasks"] == []
    assert diff["revalidation_tasks"] == []
    assert state.get_meta("plan.pending_revalidation") == []
    assert "no signature for task S6-T1" in capsys.readouterr().out


def make_roadmap_with_only_newest(root: Path) -> Roadmap:
    write(
        root / "tasks/roadmap.yaml",
        """project: revalidation-test
sprints:
  s6:
    tasks:
      S6-T1: {file: tasks/S6-T1.md, stage: 1}
""",
    )
    return Roadmap(root, root / "tasks/roadmap.yaml")
