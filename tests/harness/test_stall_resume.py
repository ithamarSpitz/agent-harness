from __future__ import annotations

from pathlib import Path

from harness.models import TaskStatus
from harness.scheduler import Scheduler
from harness.runner import AgentResult
from tests.harness.test_quota_plan_repair import init_repo


class StallOnceReviewRunner:
    def __init__(self) -> None:
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
                False, "", "stalled", stalled=True, transient_error=True,
                waiting_cause="timeout",
            )
        return AgentResult(True, "VERDICT: PASS\n")


def test_review_stall_resume_runs_verification_once(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path, review=True)
    original_verify_task = verifier.verify_task
    verification_calls = 0

    def counted_verify_task(*args, **kwargs):
        nonlocal verification_calls
        verification_calls += 1
        return original_verify_task(*args, **kwargs)

    verifier.verify_task = counted_verify_task
    runner = StallOnceReviewRunner()
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)
    sprint = roadmap.sprint("s1")

    assert not scheduler.run_sprint(sprint)
    runtime = state.get("A")
    assert runtime.status == TaskStatus.WAITING_TIMEOUT
    assert runtime.waiting_phase == "review"
    assert runtime.attempt == 1
    assert runner.implement_calls == 1
    assert runner.review_calls == 1
    assert verification_calls == 1

    assert scheduler.run_sprint(sprint)
    assert state.get("A").status == TaskStatus.DONE
    assert runner.implement_calls == 1
    assert runner.review_calls == 2
    assert verification_calls == 1


def test_review_stall_resume_reruns_verification_when_worktree_changes(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path, review=True)
    original_verify_task = verifier.verify_task
    verification_calls = 0

    def counted_verify_task(*args, **kwargs):
        nonlocal verification_calls
        verification_calls += 1
        return original_verify_task(*args, **kwargs)

    verifier.verify_task = counted_verify_task
    runner = StallOnceReviewRunner()
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)
    sprint = roadmap.sprint("s1")

    assert not scheduler.run_sprint(sprint)
    worktree = Path(state.get("A").worktree)
    (worktree / "out.txt").write_text("changed\n", encoding="utf-8")

    assert scheduler.run_sprint(sprint)
    assert state.get("A").status == TaskStatus.DONE
    assert runner.implement_calls == 1
    assert runner.review_calls == 2
    assert verification_calls == 2
