from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from harness.config import HarnessConfig
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.models import TaskStatus
from harness.plan_change import PlanChangeManager
from harness.quota import QuotaManager
from harness.roadmap import Roadmap
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier
from tests.portable import file_exists_command, yaml_quote


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def init_repo(root: Path, *, review: bool = False) -> tuple[HarnessConfig, Roadmap, StateStore, WorktreeManager, Verifier, FailureStore]:
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    write(root / ".gitignore", ".harness/\n")
    write(root / "AGENTS.md", "# test\n")
    write(root / "ARCHITECTURE.md", "# architecture\n")
    write(root / "harness.yaml", """
execution:
  max_parallel_agents: 1
  default_max_attempts: 1
  default_timeout_minutes: 1
cursor:
  command: agent
  output_format: text
  models: {worker: null, reviewer: null, escalation: null}
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths: {protected: [], review_required: []}
quota:
  policy: stop
  probe_interval_minutes: 60
plan_repair:
  enabled: true
  planner_model_class: escalation
  timeout_minutes: 1
""")
    verify_out = file_exists_command("out.txt")
    write(root / "tasks/A.md", f"""---
allowed_paths: [out.txt]
verification: [{yaml_quote(verify_out)}]
review: {str(review).lower()}
---
# A
""")
    write(root / "tasks/roadmap.yaml", f"""
project: test
sprints:
  s1:
    tasks:
      A: {{file: tasks/A.md, stage: 1, depends_on: []}}
    stage_verification:
      "1": [{yaml_quote(verify_out)}]
""")
    run(["git", "add", "."], root)
    run(["git", "commit", "-m", "initial"], root)
    config = HarnessConfig.load(root / "harness.yaml")
    roadmap = Roadmap(root, root / "tasks/roadmap.yaml")
    state = StateStore(root / ".harness/state.json")
    worktrees = WorktreeManager(root, root / ".harness")
    verifier = Verifier(config, root / ".harness/logs", worktrees)
    failures = FailureStore(root / ".harness/failures")
    return config, roadmap, state, worktrees, verifier, failures


class QuotaOnceImplementRunner:
    def __init__(self):
        self.implement_calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.implement_calls += 1
        if self.implement_calls == 1:
            return AgentResult(False, "usage limit reached", "quota", quota_exhausted=True)
        (workspace / "out.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


class QuotaOnceReviewRunner:
    def __init__(self):
        self.implement_calls = 0
        self.review_calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.implement_calls += 1
        (workspace / "out.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        self.review_calls += 1
        if self.review_calls == 1:
            return AgentResult(False, "quota exceeded", "quota", quota_exhausted=True)
        return AgentResult(True, "VERDICT: PASS\n")


def test_quota_pause_does_not_consume_implementation_attempt(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path, review=False)
    runner = QuotaOnceImplementRunner()
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)

    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.WAITING_FOR_QUOTA
    assert state.get("A").attempt == 0
    assert failures.get("s1")["kind"] == "QUOTA_WAIT"

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("A").attempt == 1
    assert runner.implement_calls == 2


def test_quota_during_review_resumes_review_without_rerunning_implementer(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path, review=True)
    runner = QuotaOnceReviewRunner()
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)

    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.WAITING_FOR_QUOTA
    assert state.get("A").waiting_phase == "review"
    assert state.get("A").attempt == 1

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.DONE
    assert runner.implement_calls == 1
    assert runner.review_calls == 2


def test_quota_reset_override_and_wait_calculation(tmp_path: Path):
    write(tmp_path / "harness.yaml", "cursor: {models: {worker: null}}\nquota: {policy: wait, probe_interval_minutes: 60, reset_grace_minutes: 5}\n")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    quota = QuotaManager(config, state)
    quota.set_reset_at("2030-01-01T10:00:00+00:00")
    seconds = quota.next_wait_seconds(now=datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc))
    assert seconds == 65 * 60
    quota.clear_reset_at()
    assert quota.next_wait_seconds(now=datetime(2030, 1, 1, 9, 0, tzinfo=timezone.utc)) == 60 * 60


class PlanRunner:
    def plan_change(self, *args, **kwargs):
        return AgentResult(
            True,
            "CHANGE_KIND: TASK_ADDITION\nEVIDENCE\nmissing prerequisite\nPROPOSED_NEW_TASKS\nB\n",
        )


def test_plan_change_adds_task_revision_and_resets_only_affected_blocker(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path, review=False)
    manager = PlanChangeManager(tmp_path, tmp_path / ".harness", state, failures)
    manager.ensure_baseline(roadmap)
    state.get("A").status = TaskStatus.BLOCKED
    state.get("A").attempt = 1
    state.save()

    request = manager.propose(
        roadmap=roadmap,
        runner=PlanRunner(),
        sprint_id="s1",
        task_id="A",
        workspace=tmp_path,
        failure_summary="missing prerequisite",
        timeout_minutes=1,
        model_class="escalation",
    )
    assert request.kind == "TASK_ADDITION"
    assert manager.open_request_id() == request.id

    verify_helper = file_exists_command("helper.txt")
    write(tmp_path / "tasks/B.md", f"""---
allowed_paths: [helper.txt]
verification: [{yaml_quote(verify_helper)}]
review: false
---
# B
""")
    verify_out = file_exists_command("out.txt")
    write(tmp_path / "tasks/roadmap.yaml", f"""
project: test
sprints:
  s1:
    tasks:
      B: {{file: tasks/B.md, stage: 1, depends_on: []}}
      A: {{file: tasks/A.md, stage: 2, depends_on: [B]}}
    stage_verification:
      "1": []
      "2": [{yaml_quote(verify_out)}]
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "plan repair"], tmp_path)
    revised = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    diff = manager.apply(request.id, revised, affected_task_ids=["A"], regenerate_contexts=False)

    assert diff["added_tasks"] == ["B"]
    assert "A" in diff["changed_tasks"]
    assert manager.revision() == 2
    assert manager.open_request_id() is None
    assert state.get("A").status == TaskStatus.PENDING
    assert state.get("A").attempt == 0


def test_plan_change_keeps_integrated_task_done_and_marks_revalidation(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path, review=False)
    manager = PlanChangeManager(tmp_path, tmp_path / ".harness", state, failures)
    manager.ensure_baseline(roadmap)
    state.get("A").status = TaskStatus.DONE
    state.save()

    # Synthetic approved request: this test focuses on apply semantics.
    rid = "CR-0001"
    manager.request_path(rid).write_text(
        '{"id":"CR-0001","sprint":"s1","task_id":"A","kind":"ARCHITECTURE_CHANGE","status":"PROPOSED","created_at":"x","failure_summary":"x","planner_output":"x","plan_revision_before":1}\n',
        encoding="utf-8",
    )
    state.set_meta("plan.open_change_request", rid)
    diff = manager.apply(rid, roadmap, affected_task_ids=["A"], regenerate_contexts=False)
    assert state.get("A").status == TaskStatus.DONE
    assert state.get_meta("plan.pending_revalidation") == ["A"]
    assert diff["revalidation_tasks"] == ["A"]


def test_plan_apply_archives_old_affected_worktree_and_rebases_future_retry(tmp_path: Path):
    from harness.cli import build_runtime, cmd_plan_change_apply

    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path, review=False)
    manager = PlanChangeManager(tmp_path, tmp_path / ".harness", state, failures)
    manager.ensure_baseline(roadmap)

    old_head = worktrees.head_commit()
    branch, wt = worktrees.create("A", old_head)
    (wt / "out.txt").write_text("partial old-plan work\n", encoding="utf-8")
    rt = state.get("A")
    rt.status = TaskStatus.BLOCKED
    rt.attempt = 1
    rt.branch = branch
    rt.worktree = str(wt)
    rt.base_ref = old_head
    state.save()

    rid = "CR-0001"
    manager.request_path(rid).write_text(
        '{"id":"CR-0001","sprint":"s1","task_id":"A","kind":"TASK_ADDITION","status":"PROPOSED","created_at":"x","failure_summary":"x","planner_output":"x","plan_revision_before":1}\n',
        encoding="utf-8",
    )
    state.set_meta("plan.open_change_request", rid)

    verify_helper = file_exists_command("helper.txt")
    write(tmp_path / "tasks/B.md", f"""---
allowed_paths: [helper.txt]
verification: [{yaml_quote(verify_helper)}]
review: false
---
# B
""")
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      B: {file: tasks/B.md, stage: 1, depends_on: []}
      A: {file: tasks/A.md, stage: 2, depends_on: [B]}
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "approved plan revision"], tmp_path)

    runtime = build_runtime(tmp_path, tmp_path / "tasks/roadmap.yaml", tmp_path / "harness.yaml")
    assert cmd_plan_change_apply(runtime, rid, ["A"]) == 0
    refreshed = runtime.state.get("A")
    assert refreshed.status == TaskStatus.PENDING
    assert refreshed.worktree is None
    assert refreshed.branch is None
    assert refreshed.base_ref is None
    assert not wt.exists()
    patch = tmp_path / ".harness/change_requests/CR-0001-A-pre-revision.patch"
    assert patch.exists()
    assert "partial old-plan work" in patch.read_text(encoding="utf-8")

class CapacityOnceImplementRunner:
    def __init__(self):
        self.implement_calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.implement_calls += 1
        if self.implement_calls == 1:
            return AgentResult(
                False,
                "RetriableError: [resource_exhausted] Error",
                "capacity",
                capacity_exhausted=True,
            )
        (workspace / "out.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


class CapacityOnceReviewRunner:
    def __init__(self):
        self.implement_calls = 0
        self.review_calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.implement_calls += 1
        (workspace / "out.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        self.review_calls += 1
        if self.review_calls == 1:
            return AgentResult(
                False,
                "RetriableError: [resource_exhausted] Error",
                "capacity",
                capacity_exhausted=True,
            )
        return AgentResult(True, "VERDICT: PASS\n")


def test_capacity_pause_does_not_consume_implementation_attempt(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path, review=False)
    runner = CapacityOnceImplementRunner()
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)

    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.WAITING_FOR_CAPACITY
    assert state.get("A").attempt == 0
    assert failures.get("s1")["kind"] == "CAPACITY_WAIT"

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("A").attempt == 1
    assert runner.implement_calls == 2


def test_capacity_during_review_resumes_review_without_rerunning_implementer(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path, review=True)
    runner = CapacityOnceReviewRunner()
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)

    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.WAITING_FOR_CAPACITY
    assert state.get("A").waiting_phase == "review"
    assert state.get("A").attempt == 1

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.DONE
    assert runner.implement_calls == 1
    assert runner.review_calls == 2
