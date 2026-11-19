from __future__ import annotations

from pathlib import Path

import pytest

from harness.models import TaskStatus
from harness.runner import AgentResult
from tests.harness.test_scheduler_integration import (
    _protected_project,
    _protected_scheduler,
)


class ResultRunner:
    def __init__(self, result: AgentResult) -> None:
        self.result = result

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        return self.result


@pytest.mark.parametrize(
    "result",
    [
        AgentResult(False, "", capacity_exhausted=True, provider="cursor"),
        AgentResult(False, "", transient_error=True, provider="cursor"),
        AgentResult(False, "", launch_failure=True, provider="cursor"),
        AgentResult(False, "", config_fault=True, provider="cursor"),
        AgentResult(False, "", quota_exhausted=True, provider="cursor"),
    ],
)
def test_non_attempt_results_are_refunded(tmp_path: Path, result: AgentResult) -> None:
    base = _protected_project(tmp_path, "true")
    scheduler, task, state = _protected_scheduler(tmp_path, ResultRunner(result))

    scheduler._run_task(task, base)

    assert state.get("A").attempt == 0


def test_failed_implementation_consumes_an_attempt(tmp_path: Path) -> None:
    base = _protected_project(tmp_path, "true")
    scheduler, task, state = _protected_scheduler(
        tmp_path, ResultRunner(AgentResult(False, "", "verification failed"))
    )
    task.max_attempts = 1

    scheduler._run_task(task, base)

    assert state.get("A").attempt == 1
    assert state.get("A").status == TaskStatus.BLOCKED
