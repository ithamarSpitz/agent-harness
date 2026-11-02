from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from harness.admission import Admission, can_start
from harness.agent_ledger import AgentRecord, Ledger, WaitingRecord


NOW = datetime(2026, 11, 3, 12, 0, 0, tzinfo=timezone.utc)
PROJECT_A = "/repos/project-a"
PROJECT_B = "/repos/booknook"


def _agent(
    project: str,
    estimate_gb: float,
    *,
    started_at: datetime | None = None,
    task_id: str = "T1",
    harness_pid: int = 1,
) -> AgentRecord:
    when = started_at if started_at is not None else NOW - timedelta(minutes=10)
    return AgentRecord(
        project=project,
        task_id=task_id,
        harness_pid=harness_pid,
        estimate_gb=estimate_gb,
        started_at=when.isoformat(),
    )


def _waiting(project: str, ready_tasks: int = 1, harness_pid: int = 1) -> WaitingRecord:
    return WaitingRecord(
        project=project,
        harness_pid=harness_pid,
        ready_tasks=ready_tasks,
        since=NOW.isoformat(),
    )


def _ledger(
    agents: list[AgentRecord] | None = None,
    waiting: list[WaitingRecord] | None = None,
) -> Ledger:
    agent_map = {f"a{index}": record for index, record in enumerate(agents or [])}
    waiting_map = {f"w{index}": record for index, record in enumerate(waiting or [])}
    return Ledger(agents=agent_map, waiting=waiting_map)


def _decide(
    *,
    project: str = PROJECT_A,
    free_gb: float = 16.0,
    estimate_gb: float = 2.0,
    min_free_gb: float = 2.0,
    max_parallel_agents: int = 3,
    warmup_minutes: float = 3.0,
    ledger: Ledger | None = None,
    now: datetime = NOW,
) -> Admission:
    return can_start(
        project=project,
        free_gb=free_gb,
        estimate_gb=estimate_gb,
        min_free_gb=min_free_gb,
        max_parallel_agents=max_parallel_agents,
        warmup_minutes=warmup_minutes,
        ledger=ledger if ledger is not None else _ledger(),
        now=now,
    )


@pytest.mark.parametrize(
    ("running", "expected"),
    [
        (0, True),
        (1, True),
        (2, True),
        (3, False),
    ],
)
def test_plenty_of_memory_one_project_until_max_parallel(running: int, expected: bool):
    agents = [_agent(PROJECT_A, 1.5, task_id=f"T{i}") for i in range(running)]
    result = _decide(ledger=_ledger(agents), max_parallel_agents=3)
    assert result.allowed is expected
    if not expected:
        assert result.reason


def test_project_with_no_agents_yes_even_with_little_memory():
    result = _decide(free_gb=0.5, estimate_gb=2.0, min_free_gb=2.0, ledger=_ledger())
    assert result.allowed is True
    assert "available" in result.reason
    assert "needed" in result.reason


@pytest.mark.parametrize(
    ("age_minutes", "expected_allowed"),
    [
        (1.0, False),
        (2.9, False),
        (3.0, True),
        (10.0, True),
    ],
)
def test_warmup_reserves_estimate_until_warmup_minutes(
    age_minutes: float, expected_allowed: bool
):
    # free 5, min_free 2 → 3 usable before reservation.
    # Warm agent reserves 2.0 → available 1.0 < estimate 1.5 while warming.
    # After warmup, reserved drops → available 3.0 >= 1.5.
    agents = [
        _agent(
            PROJECT_B,
            2.0,
            started_at=NOW - timedelta(minutes=age_minutes),
            task_id="warm",
        ),
        _agent(PROJECT_A, 1.5, task_id="own"),
    ]
    result = _decide(
        free_gb=5.0,
        estimate_gb=1.5,
        min_free_gb=2.0,
        warmup_minutes=3.0,
        ledger=_ledger(agents),
    )
    assert result.allowed is expected_allowed
    if not expected_allowed:
        assert "available" in result.reason
        assert "needed" in result.reason


def test_fairness_project_with_more_agents_yields():
    agents = [
        _agent(PROJECT_A, 1.5, task_id="a1"),
        _agent(PROJECT_A, 1.5, task_id="a2"),
        _agent(PROJECT_B, 1.5, task_id="b1"),
    ]
    waiting = [_waiting(PROJECT_A, ready_tasks=1), _waiting(PROJECT_B, ready_tasks=1)]
    ledger = _ledger(agents, waiting)

    for_a = _decide(project=PROJECT_A, free_gb=10.0, estimate_gb=1.5, ledger=ledger)
    for_b = _decide(project=PROJECT_B, free_gb=10.0, estimate_gb=1.5, ledger=ledger)

    assert for_a.allowed is False
    assert "booknook" in for_a.reason
    assert "waiting" in for_a.reason
    assert for_b.allowed is True


def test_every_no_has_a_reason():
    cases = [
        _decide(
            ledger=_ledger([_agent(PROJECT_A, 1.0, task_id=f"T{i}") for i in range(3)]),
            max_parallel_agents=3,
        ),
        _decide(
            free_gb=3.0,
            estimate_gb=2.0,
            min_free_gb=2.0,
            ledger=_ledger([_agent(PROJECT_A, 1.0)]),
        ),
        _decide(
            project=PROJECT_A,
            free_gb=10.0,
            estimate_gb=1.5,
            ledger=_ledger(
                [
                    _agent(PROJECT_A, 1.5, task_id="a1"),
                    _agent(PROJECT_A, 1.5, task_id="a2"),
                    _agent(PROJECT_B, 1.5, task_id="b1"),
                ],
                [_waiting(PROJECT_B)],
            ),
        ),
    ]
    for result in cases:
        assert result.allowed is False
        assert result.reason.strip()
