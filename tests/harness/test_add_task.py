from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path

import yaml

from harness.cli import (
    _cmd_run_live,
    build_runtime,
    cmd_inspect,
    cmd_plan_change_propose,
    cmd_reset_task,
    cmd_rerun_blocker,
    cmd_review,
    cmd_status,
    cmd_verify,
    main,
)
from harness.config import HarnessConfig
from harness.git_worktree import WorktreeManager
from harness.models import TaskStatus
from harness.roadmap import Roadmap
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.task_queue import TaskQueueError
from harness.verifier import Verifier
from tests.portable import fail_command, file_exists_command, yaml_quote


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def base_harness_yaml(extra: str = "", *, poll_seconds: int = 1, models: str = "{}") -> str:
    return f"""
execution:
  max_parallel_agents: 2
  default_max_attempts: 1
  default_timeout_minutes: 1
  watchdog_poll_seconds: {poll_seconds}
  keep_successful_worktrees: false
cursor:
  command: agent
  output_format: text
  models: {models}
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths:
  protected: []
  review_required: []
{extra}
"""


def init_repo(tmp_path: Path) -> None:
    run(["git", "init", "-b", "main"], tmp_path)
    run(["git", "config", "user.name", "Test"], tmp_path)
    run(["git", "config", "user.email", "test@example.com"], tmp_path)
    write(tmp_path / ".gitignore", ".harness/\nextra/\n")
    write(tmp_path / "AGENTS.md", "# test\n")


class FakeRunner:
    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        out = workspace / "out"
        out.mkdir(exist_ok=True)
        (out / f"{task.id}.txt").write_text(task.id + "\n", encoding="utf-8")
        from harness.runner import AgentResult

        return AgentResult(True, "ok")

    def review(self, task, workspace, base_ref, verification_summary, timeout_minutes, **kwargs):
        from harness.runner import AgentResult

        return AgentResult(True, "VERDICT: PASS\n")

    def plan_change(self, *args, **kwargs):
        from harness.runner import AgentResult

        return AgentResult(True, "CHANGE_KIND: IMPLEMENTATION_ONLY\n")


class FailingIdsRunner(FakeRunner):
    def __init__(self, fail_ids: set[str]) -> None:
        self.fail_ids = set(fail_ids)

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        if task.id in self.fail_ids:
            from harness.runner import AgentResult

            return AgentResult(False, "synthetic fail", "synthetic fail")
        return super().implement(task, workspace, timeout_minutes, attempt, previous_failure, **kwargs)

class SlowRunner(FakeRunner):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.started.set()
        if not self.release.wait(30):
            from harness.runner import AgentResult

            return AgentResult(False, "timed out waiting for release")
        return super().implement(task, workspace, timeout_minutes, attempt, previous_failure, **kwargs)


def build_scheduler(tmp_path: Path, runner) -> tuple[Scheduler, Roadmap, StateStore]:
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    verifier = Verifier(config, tmp_path / ".harness/logs", worktrees)
    scheduler = Scheduler(
        tmp_path,
        config,
        state,
        worktrees,
        runner,
        verifier,
        roadmap=roadmap,
        roadmap_path=tmp_path / "tasks/roadmap.yaml",
    )
    return scheduler, roadmap, state


def task_brief(task_id: str, allowed: str, verify_cmd: str, *, depends_on: list[str] | None = None) -> str:
    deps = depends_on or []
    deps_yaml = json.dumps(deps)
    return f"""---
id: {task_id}
allowed_paths: [{yaml_quote(allowed)}]
verification: [{yaml_quote(verify_cmd)}]
review: false
depends_on: {deps_yaml}
---
# {task_id}
"""


def test_added_task_joins_running_stage(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    runner = SlowRunner()
    scheduler, roadmap, state = build_scheduler(tmp_path, runner)
    queue = scheduler.task_queue
    verify_d = file_exists_command("out/D.txt")
    add_brief = tmp_path / "extra/D-add.md"
    write(add_brief, task_brief("D", "out/D.txt", verify_d))

    def run_sprint() -> None:
        assert scheduler.run_sprint(roadmap.sprint("s1"))

    thread = threading.Thread(target=run_sprint)
    thread.start()
    assert runner.started.wait(10)
    queue.enqueue(add_brief, "s1", roadmap, stage_choice="auto")
    runner.release.set()
    thread.join(60)
    state.load()

    assert state.get("A").status == TaskStatus.DONE
    assert state.get("D").status == TaskStatus.DONE
    assert state.get("D").added
    assert state.get("D").brief_path is not None
    assert "queue/accepted" in Path(state.get("D").brief_path).as_posix()
    assert roadmap.sprint("s1").tasks["D"].stage == 1
    assert (tmp_path / "out/D.txt").exists()


def test_overlap_auto_next_stage(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    verify_b = file_exists_command("out/B.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
      "2": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    runner = SlowRunner()
    scheduler, roadmap, state = build_scheduler(tmp_path, runner)
    write(
        tmp_path / "extra/B-add.md",
        task_brief("B", "out/**", verify_b),
    )

    thread = threading.Thread(target=lambda: scheduler.run_sprint(roadmap.sprint("s1")))
    thread.start()
    assert runner.started.wait(10)
    scheduler.task_queue.enqueue(tmp_path / "extra/B-add.md", "s1", roadmap, stage_choice="auto")
    runner.release.set()
    thread.join(60)
    state.load()

    assert state.get("B").added
    assert roadmap.sprint("s1").tasks["B"].stage == 2
    assert state.get("B").status == TaskStatus.DONE


def test_current_conflict_rejected(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    runner = SlowRunner()
    scheduler, roadmap, _state = build_scheduler(tmp_path, runner)
    verify_x = file_exists_command("out/X.txt")
    write(tmp_path / "extra/X-add.md", task_brief("X", "out/**", verify_x))

    thread = threading.Thread(target=lambda: scheduler.run_sprint(roadmap.sprint("s1")))
    thread.start()
    assert runner.started.wait(10)
    scheduler.task_queue.enqueue(tmp_path / "extra/X-add.md", "s1", roadmap, stage_choice="current")
    runner.release.set()
    thread.join(60)

    reason = (tmp_path / ".harness/queue/rejected/X.reason.txt").read_text(encoding="utf-8")
    assert "conflict" in reason.lower()
    assert "X" not in roadmap.sprint("s1").tasks


def test_next_on_last_stage_creates_new_stage(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
      "2": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, state = build_scheduler(tmp_path, FakeRunner())
    verify_n = file_exists_command("out/N.txt")
    write(tmp_path / "extra/N-add.md", task_brief("N", "out/N.txt", verify_n))
    scheduler.task_queue.enqueue(tmp_path / "extra/N-add.md", "s1", roadmap, stage_choice="next")
    scheduler.task_queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"]],
        merge_in_progress=False,
        scheduler=scheduler,
    )
    assert roadmap.sprint("s1").tasks["N"].stage == 2
    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("N").status == TaskStatus.DONE


def test_invalid_brief_refused_at_enqueue(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, state = build_scheduler(tmp_path, FakeRunner())
    queue = scheduler.task_queue
    head_before = run(["git", "rev-parse", "HEAD"], tmp_path)

    def assert_refused(path: Path, sprint_id: str, needle: str) -> None:
        try:
            queue.enqueue(path, sprint_id, roadmap)
        except TaskQueueError as exc:
            assert needle.lower() in str(exc).lower()
        else:
            raise AssertionError(f"expected enqueue of {path} to fail")
        assert list((tmp_path / ".harness/queue").glob("*.json")) == []

    write(tmp_path / "extra/no-fm.md", "# not a brief\n")
    assert_refused(tmp_path / "extra/no-fm.md", "s1", "frontmatter")

    write(tmp_path / "extra/unclosed.md", "---\nid: UNCLOSED\nallowed_paths: [out/U.txt]\n")
    assert_refused(tmp_path / "extra/unclosed.md", "s1", "frontmatter")

    write(tmp_path / "extra/empty-paths.md", "---\nid: BAD\nallowed_paths: []\n---\n")
    assert_refused(tmp_path / "extra/empty-paths.md", "s1", "allowed_paths")

    write(tmp_path / "extra/dup.md", task_brief("A", "out/A2.txt", file_exists_command("out/A2.txt")))
    assert_refused(tmp_path / "extra/dup.md", "s1", "already exists")

    write(
        tmp_path / "extra/unk.md",
        task_brief("U", "out/U.txt", file_exists_command("out/U.txt"), depends_on=["NOPE"]),
    )
    assert_refused(tmp_path / "extra/unk.md", "s1", "unknown")

    assert_refused(tmp_path / "extra/empty-paths.md", "missing-sprint", "unknown sprint")

    assert run(["git", "rev-parse", "HEAD"], tmp_path) == head_before
    assert not state.path.exists() or json.loads(state.path.read_text())["tasks"] == {}

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    write(tmp_path / "extra/late.md", task_brief("L", "out/L.txt", file_exists_command("out/L.txt")))
    assert_refused(tmp_path / "extra/late.md", "s1", "already complete")
    assert list((tmp_path / ".harness/queue").glob("*.json")) == []


def test_enqueue_does_not_write_state_or_dirty_git(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml(poll_seconds=30))
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    runner = SlowRunner()
    scheduler, roadmap, _ = build_scheduler(tmp_path, runner)
    verify_d = file_exists_command("out/D.txt")
    add_brief = tmp_path / "extra/D-add.md"
    write(add_brief, task_brief("D", "out/D.txt", verify_d))

    thread = threading.Thread(target=lambda: scheduler.run_sprint(roadmap.sprint("s1")))
    thread.start()
    assert runner.started.wait(10)
    state_before = (tmp_path / ".harness/state.json").read_text(encoding="utf-8")
    git_before = run(["git", "status", "--porcelain"], tmp_path)
    assert main(
        [
            "--root",
            str(tmp_path),
            "--roadmap",
            "tasks/roadmap.yaml",
            "--config",
            "harness.yaml",
            "add-task",
            "s1",
            "extra/D-add.md",
        ]
    ) == 0
    assert (tmp_path / ".harness/state.json").read_text(encoding="utf-8") == state_before
    assert run(["git", "status", "--porcelain"], tmp_path) == git_before
    runner.release.set()
    thread.join(60)
    assert run(["git", "status", "--porcelain"], tmp_path) == ""


def test_resume_keeps_accepted_added_task(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    verify_d = file_exists_command("out/D.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, state = build_scheduler(tmp_path, FakeRunner())
    write(tmp_path / "extra/D-add.md", task_brief("D", "out/D.txt", verify_d))
    scheduler.task_queue.enqueue(tmp_path / "extra/D-add.md", "s1", roadmap)
    scheduler.task_queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"]],
        merge_in_progress=False,
        scheduler=scheduler,
    )
    assert state.get("D").added

    state2 = StateStore(tmp_path / ".harness/state.json")
    roadmap2 = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    scheduler2, _, _ = build_scheduler(tmp_path, FakeRunner())
    sprint = roadmap2.sprint("s1")
    scheduler2.task_queue.merge_added_tasks_from_state(sprint)
    assert "D" in sprint.tasks
    assert scheduler2.run_sprint(sprint)
    state2.load()
    assert state2.get("D").status == TaskStatus.DONE


def test_completed_sprint_records_added_tasks_in_repo(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    verify_d = file_exists_command("out/D.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, state = build_scheduler(tmp_path, FakeRunner())
    write(tmp_path / "extra/D-add.md", task_brief("D", "out/D.txt", verify_d))
    scheduler.task_queue.enqueue(tmp_path / "extra/D-add.md", "s1", roadmap)
    assert scheduler.run_sprint(roadmap.sprint("s1"))
    raw = yaml.safe_load((tmp_path / "tasks/roadmap.yaml").read_text(encoding="utf-8"))
    assert "D" in raw["sprints"]["s1"]["tasks"]
    log = run(["git", "log", "-1", "--format=%s"], tmp_path)
    assert "record added tasks" in log
    names = run(["git", "show", "--name-only", "--format=", "HEAD"], tmp_path)
    assert "tasks/D.md" in names


def test_failed_sprint_does_not_record_added_tasks(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    fail = fail_command()
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)
    roadmap_before = (tmp_path / "tasks/roadmap.yaml").read_text(encoding="utf-8")

    scheduler, roadmap, _state = build_scheduler(tmp_path, FakeRunner())
    write(tmp_path / "extra/B-add.md", task_brief("B", "out/B.txt", fail))
    scheduler.task_queue.enqueue(tmp_path / "extra/B-add.md", "s1", roadmap)
    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert (tmp_path / "tasks/roadmap.yaml").read_text(encoding="utf-8") == roadmap_before


def test_failing_added_task_rolls_back_stage(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    fail = fail_command()
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)
    base_commit = run(["git", "rev-parse", "HEAD"], tmp_path)

    scheduler, roadmap, state = build_scheduler(tmp_path, FakeRunner())
    write(tmp_path / "extra/B-add.md", task_brief("B", "out/B.txt", fail))
    scheduler.task_queue.enqueue(tmp_path / "extra/B-add.md", "s1", roadmap)
    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert run(["git", "rev-parse", "HEAD"], tmp_path) == base_commit
    assert state.get("A").status == TaskStatus.VERIFIED


def test_merge_in_progress_rejects_current(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, _state = build_scheduler(tmp_path, FakeRunner())
    write(tmp_path / "extra/C-add.md", task_brief("C", "out/C.txt", file_exists_command("out/C.txt")))
    scheduler.task_queue.enqueue(tmp_path / "extra/C-add.md", "s1", roadmap, stage_choice="current")
    scheduler.task_queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"]],
        merge_in_progress=True,
        scheduler=scheduler,
    )
    reason = (tmp_path / ".harness/queue/rejected/C.reason.txt").read_text(encoding="utf-8")
    assert "merge" in reason.lower()
    assert "C" not in roadmap.sprint("s1").tasks

    write(tmp_path / "extra/N-add.md", task_brief("N", "out/N.txt", file_exists_command("out/N.txt")))
    scheduler.task_queue.enqueue(tmp_path / "extra/N-add.md", "s1", roadmap, stage_choice="auto")
    scheduler.task_queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"]],
        merge_in_progress=True,
        scheduler=scheduler,
    )
    assert roadmap.sprint("s1").tasks["N"].stage == 2


def test_pickup_after_last_stage_done_places_auto_and_next(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, state = build_scheduler(tmp_path, FakeRunner())
    sprint = roadmap.sprint("s1")
    write(tmp_path / "extra/AUTO-add.md", task_brief("AUTO", "out/AUTO.txt", file_exists_command("out/AUTO.txt")))
    write(tmp_path / "extra/NEXT-add.md", task_brief("NEXT", "out/NEXT.txt", file_exists_command("out/NEXT.txt")))
    write(tmp_path / "extra/CUR-add.md", task_brief("CUR", "out/CUR.txt", file_exists_command("out/CUR.txt")))
    scheduler.task_queue.enqueue(tmp_path / "extra/AUTO-add.md", "s1", roadmap, stage_choice="auto")
    scheduler.task_queue.enqueue(tmp_path / "extra/NEXT-add.md", "s1", roadmap, stage_choice="next")
    scheduler.task_queue.enqueue(tmp_path / "extra/CUR-add.md", "s1", roadmap, stage_choice="current")

    state.get("A").status = TaskStatus.DONE
    state.save()
    scheduler.task_queue.process_pending(
        sprint,
        roadmap,
        current_stage=1,
        stage_tasks=[sprint.tasks["A"]],
        merge_in_progress=True,
        scheduler=scheduler,
    )
    assert sprint.tasks["AUTO"].stage == 2
    assert sprint.tasks["NEXT"].stage == 2
    assert "CUR" not in sprint.tasks
    reason = (tmp_path / ".harness/queue/rejected/CUR.reason.txt").read_text(encoding="utf-8")
    assert "merge" in reason.lower()
    assert "complete" not in reason.lower()
    assert not (tmp_path / ".harness/queue/rejected/AUTO.reason.txt").exists()
    assert not (tmp_path / ".harness/queue/rejected/NEXT.reason.txt").exists()


def test_auto_queued_during_last_stage_merge_runs_in_new_stage(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, state = build_scheduler(tmp_path, FakeRunner())
    write(tmp_path / "extra/N-add.md", task_brief("N", "out/N.txt", file_exists_command("out/N.txt")))
    original = scheduler._integrate_stage
    queued = {"done": False}

    def enqueue_during_merge(sprint, stage, tasks, checkpoint):
        if not queued["done"]:
            scheduler.task_queue.enqueue(
                tmp_path / "extra/N-add.md", "s1", roadmap, stage_choice="auto"
            )
            queued["done"] = True
        return original(sprint, stage, tasks, checkpoint)

    scheduler._integrate_stage = enqueue_during_merge
    sprint = roadmap.sprint("s1")
    assert scheduler.run_sprint(sprint)
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("N").status == TaskStatus.DONE
    assert state.get("N").added
    assert sprint.tasks["N"].stage == 2
    assert not (tmp_path / ".harness/queue/rejected/N.reason.txt").exists()


def test_status_lists_added_and_rejected(tmp_path: Path, capsys):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, state = build_scheduler(tmp_path, FakeRunner())
    write(tmp_path / "extra/D-add.md", task_brief("D", "out/D.txt", file_exists_command("out/D.txt")))
    scheduler.task_queue.enqueue(tmp_path / "extra/D-add.md", "s1", roadmap)
    scheduler.task_queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"]],
        merge_in_progress=False,
        scheduler=scheduler,
    )
    write(tmp_path / "extra/X-add.md", task_brief("X", "out/**", file_exists_command("out/X.txt")))
    scheduler.task_queue.enqueue(tmp_path / "extra/X-add.md", "s1", roadmap, stage_choice="current")
    scheduler.task_queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"], roadmap.sprint("s1").tasks["D"]],
        merge_in_progress=False,
        scheduler=scheduler,
    )

    cmd_status(roadmap, state, "s1", tmp_path / ".harness")
    out = capsys.readouterr().out
    assert "(added)" in out
    assert "D" in out
    assert "REJECTED" in out
    assert "X" in out
    assert "conflict" in out.lower()


def test_invalid_brief_rejected_at_pickup(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, _state = build_scheduler(tmp_path, FakeRunner())
    bad = tmp_path / "extra/bad.md"
    write(
        bad,
        """---
id: BAD
allowed_paths: [out/BAD.txt]
verification: [true]
max_attempts: nope
---
# bad
""",
    )
    scheduler.task_queue.enqueue(bad, "s1", roadmap)
    scheduler.task_queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"]],
        merge_in_progress=False,
        scheduler=scheduler,
    )
    assert "BAD" not in roadmap.sprint("s1").tasks
    reason = (tmp_path / ".harness/queue/rejected/BAD.reason.txt").read_text(encoding="utf-8")
    assert reason.strip()


def test_reject_same_task_twice_does_not_crash(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, _state = build_scheduler(tmp_path, FakeRunner())
    conflict = tmp_path / "extra/X-add.md"
    write(conflict, task_brief("X", "out/**", file_exists_command("out/X.txt")))

    for _ in range(2):
        scheduler.task_queue.enqueue(conflict, "s1", roadmap, stage_choice="current")
        scheduler.task_queue.process_pending(
            roadmap.sprint("s1"),
            roadmap,
            current_stage=1,
            stage_tasks=[roadmap.sprint("s1").tasks["A"]],
            merge_in_progress=False,
            scheduler=scheduler,
        )
    assert (tmp_path / ".harness/queue/rejected/X.reason.txt").is_file()
    assert "X" not in roadmap.sprint("s1").tasks


def test_next_rejects_when_dependency_is_in_later_stage(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    verify_c = file_exists_command("out/C.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/C.md", task_brief("C", "out/C.txt", verify_c))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
      C: {file: tasks/C.md, stage: 3, depends_on: []}
    stage_verification:
      "1": []
      "3": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, _state = build_scheduler(tmp_path, FakeRunner())
    write(
        tmp_path / "extra/T-add.md",
        task_brief("T", "out/T.txt", file_exists_command("out/T.txt"), depends_on=["C"]),
    )
    scheduler.task_queue.enqueue(tmp_path / "extra/T-add.md", "s1", roadmap, stage_choice="next")
    scheduler.task_queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"]],
        merge_in_progress=False,
        scheduler=scheduler,
    )
    assert "T" not in roadmap.sprint("s1").tasks
    reason = (tmp_path / ".harness/queue/rejected/T.reason.txt").read_text(encoding="utf-8")
    assert "dependency" in reason.lower()
    assert "C" in reason


def test_same_pickup_dependent_is_placed_after_predecessor(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
      "2": []
      "3": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, state = build_scheduler(tmp_path, FakeRunner())
    write(
        tmp_path / "extra/JOIN-add.md",
        task_brief(
            "JOIN",
            "out/JOIN.txt",
            file_exists_command("out/JOIN.txt"),
            depends_on=["PRE"],
        ),
    )
    write(
        tmp_path / "extra/PRE-add.md",
        task_brief("PRE", "out/PRE.txt", file_exists_command("out/PRE.txt")),
    )
    queue = scheduler.task_queue
    queue.enqueue(tmp_path / "extra/PRE-add.md", "s1", roadmap, stage_choice="next")
    queue.enqueue(tmp_path / "extra/JOIN-add.md", "s1", roadmap, stage_choice="next")
    pending_ids = [task_id for task_id, _, _ in queue.pending_entries("s1")]
    assert pending_ids == ["JOIN", "PRE"]
    queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"]],
        merge_in_progress=False,
        scheduler=scheduler,
    )
    sprint = roadmap.sprint("s1")
    assert sprint.tasks["PRE"].stage == 2
    assert sprint.tasks["JOIN"].stage == 3
    assert scheduler.run_sprint(sprint)
    assert state.get("PRE").status == TaskStatus.DONE
    assert state.get("JOIN").status == TaskStatus.DONE


def test_same_pickup_auto_dependent_is_placed_after_predecessor_when_current_unjoinable(
    tmp_path: Path,
):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
      "2": []
      "3": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, _state = build_scheduler(tmp_path, FakeRunner())
    write(
        tmp_path / "extra/JOIN-add.md",
        task_brief(
            "JOIN",
            "out/JOIN.txt",
            file_exists_command("out/JOIN.txt"),
            depends_on=["PRE"],
        ),
    )
    write(
        tmp_path / "extra/PRE-add.md",
        task_brief("PRE", "out/PRE.txt", file_exists_command("out/PRE.txt")),
    )
    queue = scheduler.task_queue
    queue.enqueue(tmp_path / "extra/PRE-add.md", "s1", roadmap, stage_choice="auto")
    queue.enqueue(tmp_path / "extra/JOIN-add.md", "s1", roadmap, stage_choice="auto")
    pending_ids = [task_id for task_id, _, _ in queue.pending_entries("s1")]
    assert pending_ids == ["JOIN", "PRE"]
    queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"]],
        merge_in_progress=True,
        scheduler=scheduler,
    )
    sprint = roadmap.sprint("s1")
    assert sprint.tasks["PRE"].stage == 2
    assert sprint.tasks["JOIN"].stage == 3


def test_overlapping_next_tasks_in_one_pickup_rejects_second(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
      "2": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, state = build_scheduler(tmp_path, FakeRunner())
    write(
        tmp_path / "extra/M-add.md",
        task_brief("M", "out/**", file_exists_command("out/M.txt")),
    )
    write(
        tmp_path / "extra/N-add.md",
        task_brief("N", "out/**", file_exists_command("out/N.txt")),
    )
    scheduler.task_queue.enqueue(tmp_path / "extra/M-add.md", "s1", roadmap, stage_choice="next")
    scheduler.task_queue.enqueue(tmp_path / "extra/N-add.md", "s1", roadmap, stage_choice="next")
    scheduler.task_queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"]],
        merge_in_progress=False,
        scheduler=scheduler,
    )
    assert roadmap.sprint("s1").tasks["M"].stage == 2
    assert "N" not in roadmap.sprint("s1").tasks
    reason = (tmp_path / ".harness/queue/rejected/N.reason.txt").read_text(encoding="utf-8")
    assert "conflict" in reason.lower()
    assert "M" in reason
    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("M").status == TaskStatus.DONE
    assert state.get("A").status == TaskStatus.DONE


def test_next_and_auto_reject_overlap_with_following_stage(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    verify_b = file_exists_command("out/B.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/B.md", task_brief("B", "out/B.txt", verify_b))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
      B: {file: tasks/B.md, stage: 2, depends_on: []}
    stage_verification:
      "1": []
      "2": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, _state = build_scheduler(tmp_path, FakeRunner())
    write(
        tmp_path / "extra/X-add.md",
        task_brief("X", "out/**", file_exists_command("out/X.txt")),
    )
    write(
        tmp_path / "extra/Y-add.md",
        task_brief("Y", "out/**", file_exists_command("out/Y.txt")),
    )
    scheduler.task_queue.enqueue(tmp_path / "extra/X-add.md", "s1", roadmap, stage_choice="next")
    scheduler.task_queue.enqueue(tmp_path / "extra/Y-add.md", "s1", roadmap, stage_choice="auto")
    scheduler.task_queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"]],
        merge_in_progress=False,
        scheduler=scheduler,
    )
    assert "X" not in roadmap.sprint("s1").tasks
    assert "Y" not in roadmap.sprint("s1").tasks
    reason = (tmp_path / ".harness/queue/rejected/X.reason.txt").read_text(encoding="utf-8")
    assert "conflict" in reason.lower()
    assert "B" in reason
    auto_reason = (tmp_path / ".harness/queue/rejected/Y.reason.txt").read_text(encoding="utf-8")
    assert "conflict" in auto_reason.lower()
    assert "B" in auto_reason


def test_current_and_auto_ignore_later_stage_accepts_in_same_pickup(tmp_path: Path):
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml())
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    write(tmp_path / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
      "2": []
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    scheduler, roadmap, _state = build_scheduler(tmp_path, FakeRunner())
    write(
        tmp_path / "extra/N1-add.md",
        task_brief("N1", "extra/**", file_exists_command("out/N1.txt")),
    )
    write(
        tmp_path / "extra/ZC-add.md",
        task_brief("ZC", "extra/zc.txt", file_exists_command("out/ZC.txt")),
    )
    write(
        tmp_path / "extra/ZA-add.md",
        task_brief("ZA", "extra/za.txt", file_exists_command("out/ZA.txt")),
    )
    queue = scheduler.task_queue
    queue.enqueue(tmp_path / "extra/N1-add.md", "s1", roadmap, stage_choice="next")
    queue.enqueue(tmp_path / "extra/ZC-add.md", "s1", roadmap, stage_choice="current")
    queue.enqueue(tmp_path / "extra/ZA-add.md", "s1", roadmap, stage_choice="auto")
    queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"]],
        merge_in_progress=False,
        scheduler=scheduler,
    )
    sprint = roadmap.sprint("s1")
    assert sprint.tasks["N1"].stage == 2
    assert sprint.tasks["ZA"].stage == 1
    assert sprint.tasks["ZC"].stage == 1
    assert not (tmp_path / ".harness/queue/rejected/ZA.reason.txt").exists()
    assert not (tmp_path / ".harness/queue/rejected/ZC.reason.txt").exists()


RECOVERY_MODELS = "{worker: null, escalation: null, reviewer: null}"


def _write_added_task_project(
    tmp_path: Path,
    *,
    extra: str = "",
    stage_verification: str | None = None,
) -> None:
    init_repo(tmp_path)
    verify_a = file_exists_command("out/A.txt")
    write(tmp_path / "harness.yaml", base_harness_yaml(extra, models=RECOVERY_MODELS))
    write(tmp_path / "tasks/A.md", task_brief("A", "out/A.txt", verify_a))
    stage_yaml = ""
    if stage_verification is not None:
        stage_yaml = f'\n      "1": [{yaml_quote(stage_verification)}]'
    write(
        tmp_path / "tasks/roadmap.yaml",
        f"""
project: test
sprints:
  s1:
    tasks:
      A: {{file: tasks/A.md, stage: 1, depends_on: []}}
    stage_verification:{stage_yaml or " {}"}
""",
    )
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)


def test_rerun_blocker_stage_barrier_includes_added_task(tmp_path: Path):
    marker = tmp_path.parent / f"{tmp_path.name}-stage-ok"
    _write_added_task_project(tmp_path, stage_verification=file_exists_command(marker))
    scheduler, roadmap, state = build_scheduler(tmp_path, FakeRunner())
    write(tmp_path / "extra/D-add.md", task_brief("D", "out/D.txt", file_exists_command("out/D.txt")))
    scheduler.task_queue.enqueue(tmp_path / "extra/D-add.md", "s1", roadmap)
    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.VERIFIED
    assert state.get("D").status == TaskStatus.VERIFIED
    assert state.get("D").added

    marker.write_text("ok\n", encoding="utf-8")
    runtime = build_runtime(tmp_path, tmp_path / "tasks/roadmap.yaml", tmp_path / "harness.yaml")
    assert "D" not in runtime.roadmap.sprint("s1").tasks
    assert cmd_rerun_blocker(runtime, "s1", None) == 0
    assert runtime.state.get("A").status == TaskStatus.DONE
    assert runtime.state.get("D").status == TaskStatus.DONE
    assert (tmp_path / "out/D.txt").exists()
    raw = yaml.safe_load((tmp_path / "tasks/roadmap.yaml").read_text(encoding="utf-8"))
    assert "D" in raw["sprints"]["s1"]["tasks"]


def test_rerun_blocker_then_resume_picks_up_task_queued_during_barrier(tmp_path: Path):
    marker = tmp_path.parent / f"{tmp_path.name}-stage-ok"
    _write_added_task_project(tmp_path, stage_verification=file_exists_command(marker))
    scheduler, roadmap, state = build_scheduler(tmp_path, FakeRunner())
    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.VERIFIED

    write(tmp_path / "extra/N-add.md", task_brief("N", "out/N.txt", file_exists_command("out/N.txt")))
    scheduler.task_queue.enqueue(tmp_path / "extra/N-add.md", "s1", roadmap, stage_choice="next")
    assert (tmp_path / ".harness/queue/N.json").exists()

    marker.write_text("ok\n", encoding="utf-8")
    runtime = build_runtime(tmp_path, tmp_path / "tasks/roadmap.yaml", tmp_path / "harness.yaml")
    runtime.runner = FakeRunner()
    runtime.scheduler.runner = FakeRunner()
    assert cmd_rerun_blocker(runtime, "s1", None) == 0
    assert runtime.state.get("A").status == TaskStatus.DONE
    assert runtime.state.get("N").added
    assert runtime.state.get("N").status != TaskStatus.DONE

    sprint = runtime.roadmap.sprint("s1")
    assert _cmd_run_live(runtime, "s1", sprint) == 0
    runtime.state.load()
    assert runtime.state.get("N").status == TaskStatus.DONE
    assert (tmp_path / "out/N.txt").exists()
    raw = yaml.safe_load((tmp_path / "tasks/roadmap.yaml").read_text(encoding="utf-8"))
    assert "N" in raw["sprints"]["s1"]["tasks"]
    assert raw["sprints"]["s1"]["tasks"]["N"]["stage"] == 2
    assert not (tmp_path / ".harness/queue/rejected/N.reason.txt").exists()


def test_rerun_blocker_runs_blocked_added_task(tmp_path: Path):
    _write_added_task_project(tmp_path)
    scheduler, roadmap, state = build_scheduler(tmp_path, FailingIdsRunner({"B"}))
    write(tmp_path / "extra/B-add.md", task_brief("B", "out/B.txt", file_exists_command("out/B.txt")))
    scheduler.task_queue.enqueue(tmp_path / "extra/B-add.md", "s1", roadmap)
    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("A").status == TaskStatus.VERIFIED
    assert state.get("B").status == TaskStatus.BLOCKED
    assert state.get("B").added
    record = scheduler.failures.get("s1")
    assert record is not None
    assert record["kind"] == "TASK_BLOCKED"
    assert "B" in record["task_ids"]

    runtime = build_runtime(tmp_path, tmp_path / "tasks/roadmap.yaml", tmp_path / "harness.yaml")
    runtime.scheduler.runner = FakeRunner()
    assert "B" not in runtime.roadmap.sprint("s1").tasks
    assert cmd_rerun_blocker(runtime, "s1", None) == 0
    assert runtime.state.get("B").status == TaskStatus.VERIFIED
    assert runtime.state.get("B").added


def test_focused_commands_resolve_added_task(tmp_path: Path, capsys):
    _write_added_task_project(
        tmp_path,
        extra="""
plan_repair:
  enabled: true
  planner_model_class: escalation
  timeout_minutes: 1
""",
    )
    scheduler, roadmap, state = build_scheduler(tmp_path, FakeRunner())
    write(tmp_path / "extra/D-add.md", task_brief("D", "out/D.txt", file_exists_command("out/D.txt")))
    scheduler.task_queue.enqueue(tmp_path / "extra/D-add.md", "s1", roadmap)
    scheduler.task_queue.process_pending(
        roadmap.sprint("s1"),
        roadmap,
        current_stage=1,
        stage_tasks=[roadmap.sprint("s1").tasks["A"]],
        merge_in_progress=False,
        scheduler=scheduler,
    )
    assert state.get("D").added

    def fresh_runtime():
        runtime = build_runtime(tmp_path, tmp_path / "tasks/roadmap.yaml", tmp_path / "harness.yaml")
        runtime.runner = FakeRunner()
        runtime.scheduler.runner = FakeRunner()
        assert "D" not in runtime.roadmap.sprint("s1").tasks
        return runtime

    inspect_runtime = fresh_runtime()
    assert cmd_inspect(inspect_runtime, "D") == 0
    inspect_out = capsys.readouterr().out
    assert '"id": "D"' in inspect_out

    verify_runtime = fresh_runtime()
    assert cmd_verify(verify_runtime, "D") == 2
    verify_err = capsys.readouterr().err
    assert "Unknown task" not in verify_err
    assert "no active worktree" in verify_err

    review_runtime = fresh_runtime()
    assert cmd_review(review_runtime, "D", 1) == 2
    review_err = capsys.readouterr().err
    assert "Unknown task" not in review_err
    assert "no active worktree" in review_err

    propose_runtime = fresh_runtime()
    assert cmd_plan_change_propose(propose_runtime, "s1", "D", None) == 0
    propose_out = capsys.readouterr().out
    assert "IMPLEMENTATION_ONLY" in propose_out

    reset_runtime = fresh_runtime()
    assert cmd_reset_task(reset_runtime, "D") == 0
    assert reset_runtime.state.get("D").status == TaskStatus.PENDING
    assert reset_runtime.state.get("D").added
    assert reset_runtime.state.get("D").brief_path is not None
