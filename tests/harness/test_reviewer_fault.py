from __future__ import annotations

from pathlib import Path

from harness.models import TaskStatus
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from tests.harness.test_launch_failure import _init_repo


class _ReviewSequenceRunner:
    manages_model_routing = False

    def __init__(self, state, reviews: list[AgentResult]):
        self.state = state
        self.reviews = list(reviews)
        self.implement_calls = 0
        self.review_calls = 0

    def implement(self, task, workspace, *args, **kwargs):
        self.implement_calls += 1
        runtime = self.state.get(task.id)
        runtime.provider_attempts["cursor"] = (
            runtime.provider_attempts.get("cursor", 0) + 1
        )
        (workspace / "out.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "implemented", provider="cursor")

    def review(self, *args, **kwargs):
        self.review_calls += 1
        return self.reviews.pop(0)

    def fallback_after_stalls(self, *args, **kwargs):
        return False


def _run_single_review_attempt(tmp_path: Path, reviews: list[AgentResult]):
    config, roadmap, state, worktrees, verifier, failures = _init_repo(
        tmp_path, both_providers=True, review=True
    )
    runner = _ReviewSequenceRunner(state, reviews)
    scheduler = Scheduler(
        tmp_path, config, state, worktrees, runner, verifier, failures=failures
    )
    task = roadmap.sprint("s1").tasks["A"]
    base_ref = worktrees.head_commit()
    branch, worktree = worktrees.create(task.id, base_ref)
    runtime = state.get(task.id)
    runtime.attempt = 1
    runtime.branch = branch
    runtime.worktree = str(worktree)
    runtime.base_ref = base_ref
    state.save()

    runner.implement(task, worktree, 1, 1, None)
    provider_attempts = dict(runtime.provider_attempts)
    outcome = scheduler._verify_review_commit(
        task,
        runtime,
        worktree,
        base_ref,
        1,
        scheduler.environment.task_env(task, worktree),
        False,
        [],
    )
    return outcome, runner, state, runtime, provider_attempts


def test_reviewer_fault_retries_elsewhere_and_passes_without_reimplementation(
    tmp_path: Path, caplog,
):
    outcome, runner, state, runtime, provider_attempts = _run_single_review_attempt(
        tmp_path,
        [
            AgentResult(False, "invalid model selection\n", provider="codex"),
            AgentResult(True, "VERDICT: PASS\n", provider="cursor"),
        ],
    )

    assert outcome is not None and outcome.ok
    assert runtime.status == TaskStatus.PASSED
    assert runner.implement_calls == 1
    assert runner.review_calls == 2
    assert runtime.attempt == 1
    assert runtime.provider_attempts == provider_attempts
    assert state.get_meta("s1.A.provider.codex.review_disabled") == (
        "invalid model selection"
    )
    assert "review fault on codex: invalid model selection; reviewing again elsewhere" in (
        caplog.text
    )


def test_second_reviewer_fail_is_normal_retry_with_its_feedback(tmp_path: Path):
    feedback = "VERDICT: FAIL\nFinding one\n"
    outcome, runner, state, runtime, provider_attempts = _run_single_review_attempt(
        tmp_path,
        [
            AgentResult(False, "invalid model selection", provider="codex"),
            AgentResult(True, feedback, provider="cursor"),
        ],
    )

    assert outcome is None
    assert runtime.status == TaskStatus.RETRY
    assert runtime.review_feedback == feedback
    assert runtime.last_error == feedback
    assert runner.implement_calls == 1
    assert runtime.attempt == 1
    assert runtime.provider_attempts == provider_attempts
    assert state.get_meta("s1.A.provider.cursor.review_disabled") is None


def test_two_reviewer_faults_block_and_list_both_without_spending_attempt(tmp_path: Path):
    outcome, runner, state, runtime, provider_attempts = _run_single_review_attempt(
        tmp_path,
        [
            AgentResult(False, "invalid model selection", provider="codex"),
            AgentResult(False, "review service misconfigured", provider="cursor"),
        ],
    )

    assert outcome is not None and not outcome.ok
    assert runtime.status == TaskStatus.BLOCKED
    assert runtime.attempt == 1
    assert runtime.provider_attempts == provider_attempts
    assert runner.implement_calls == 1
    assert runner.review_calls == 2
    assert "codex: invalid model selection" in (runtime.last_error or "")
    assert "cursor: review service misconfigured" in (runtime.last_error or "")
    assert state.get_meta("s1.A.provider.codex.review_disabled") == (
        "invalid model selection"
    )
    assert state.get_meta("s1.A.provider.cursor.review_disabled") == (
        "review service misconfigured"
    )


def test_failed_review_call_with_fail_verdict_is_not_a_reviewer_fault(tmp_path: Path):
    feedback = "VERDICT: FAIL\nConcrete finding\n"
    outcome, runner, state, runtime, provider_attempts = _run_single_review_attempt(
        tmp_path,
        [AgentResult(False, feedback, error="review exited 1", provider="codex")],
    )

    assert outcome is None
    assert runtime.status == TaskStatus.RETRY
    assert runtime.review_feedback == feedback
    assert runtime.attempt == 1
    assert runtime.provider_attempts == provider_attempts
    assert runner.review_calls == 1
    assert state.get_meta("s1.A.provider.codex.review_disabled") is None
    assert state.get_meta("s1.A.provider.codex.disabled.review") is None
