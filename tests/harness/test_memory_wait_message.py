from __future__ import annotations

import logging
import threading
from pathlib import Path

from harness.admission import Admission
from harness.models import TaskStatus
from tests.harness.test_scheduler_memory import (
    TrackingRunner,
    _scheduler,
    init_repo,
)


def _wait_messages(caplog) -> list[str]:
    return [r.message for r in caplog.records if r.message.startswith("Admission wait:")]


def _printed_waits(lines: list[str]) -> list[str]:
    return [line for line in lines if line.startswith("Waiting (memory):")]


def test_same_admission_reason_prints_once_over_five_polls(tmp_path: Path, caplog, monkeypatch):
    config, roadmap, state, worktrees, verifier, failures = init_repo(
        tmp_path, ("A", "B"), max_parallel=3, memory_per_agent_gb=2.0, min_free_memory_gb=2.0
    )
    runner = TrackingRunner()
    runner.gate("A", "B")
    printed: list[str] = []
    real_print = print

    def capturing_print(*args, **kwargs):
        text = " ".join(str(a) for a in args)
        printed.append(text)
        real_print(*args, **kwargs)

    monkeypatch.setattr("builtins.print", capturing_print)

    reason = "waiting: 0.5 GB available, 2.0 GB needed"
    decisions = [Admission(False, reason)] * 5 + [Admission(True, "ok")] * 2
    call_count = {"n": 0}

    scheduler = _scheduler(
        tmp_path,
        config,
        state,
        worktrees,
        runner,
        verifier,
        failures,
        free_memory_gb=lambda: 4.5,
    )

    five_refused = threading.Event()

    def fake_admission():
        index = min(call_count["n"], len(decisions) - 1)
        call_count["n"] += 1
        if call_count["n"] >= 5:
            five_refused.set()
        return decisions[index]

    monkeypatch.setattr(scheduler, "_admission_decision", fake_admission)

    with caplog.at_level(logging.INFO, logger="harness.scheduler"):
        thread = threading.Thread(target=lambda: scheduler.run_sprint(roadmap.sprint("s1")))
        thread.start()
        # Five refused polls (watchdog_poll_seconds=1) before starts are allowed.
        assert five_refused.wait(timeout=30)
        assert call_count["n"] >= 5
        assert _wait_messages(caplog) == [f"Admission wait: {reason}"]
        assert _printed_waits(printed) == [f"Waiting (memory): {reason}"]
        runner.wait_started("A", timeout=30)
        runner.wait_started("B", timeout=30)
        runner.release()
        thread.join(timeout=120)

    assert not thread.is_alive()
    assert all(state.get(task_id).status == TaskStatus.DONE for task_id in ("A", "B"))


def test_changed_reason_and_post_start_refusal_print_again(tmp_path: Path, caplog, monkeypatch):
    config, roadmap, state, worktrees, verifier, failures = init_repo(
        tmp_path,
        ("A", "B", "C"),
        max_parallel=3,
        memory_per_agent_gb=2.0,
        min_free_memory_gb=2.0,
    )
    runner = TrackingRunner()
    runner.gate("A", "B", "C")
    printed: list[str] = []
    real_print = print

    def capturing_print(*args, **kwargs):
        text = " ".join(str(a) for a in args)
        printed.append(text)
        real_print(*args, **kwargs)

    monkeypatch.setattr("builtins.print", capturing_print)

    reason_a = "waiting: reason-a"
    reason_b = "waiting: reason-b"
    # Refuse with reason_a (deduped), then reason_b (print again), then allow A.
    # After A starts, refuse again with the same reason_b text. That second
    # reason_b print only happens if successful start cleared last_memory_wait_reason;
    # a changed-reason path would not cover Decision 1 / AC2.
    # Then allow B and C.
    sequence: list[Admission] = [
        Admission(False, reason_a),
        Admission(False, reason_a),
        Admission(False, reason_b),
        Admission(True, "ok"),  # start A — clears last_memory_wait_reason
        Admission(False, reason_b),  # same text after start: must print again
        Admission(False, reason_b),  # same reason: no second print
        Admission(True, "ok"),  # start B
        Admission(True, "ok"),  # start C
    ]
    call_count = {"n": 0}
    reason_b_before_start = threading.Event()
    reason_b_after_start = threading.Event()
    after_start_dup_seen = threading.Event()

    scheduler = _scheduler(
        tmp_path,
        config,
        state,
        worktrees,
        runner,
        verifier,
        failures,
        free_memory_gb=lambda: 50.0,
    )

    def fake_admission():
        index = min(call_count["n"], len(sequence) - 1)
        call_count["n"] += 1
        decision = sequence[index]
        if decision.reason == reason_b and not decision.allowed:
            # Indices are 1-based after increment: 3 = pre-start, 5 = post-start, 6 = dup.
            if call_count["n"] == 3:
                reason_b_before_start.set()
            elif call_count["n"] == 5:
                reason_b_after_start.set()
            elif call_count["n"] >= 6:
                after_start_dup_seen.set()
        return decision

    monkeypatch.setattr(scheduler, "_admission_decision", fake_admission)

    with caplog.at_level(logging.INFO, logger="harness.scheduler"):
        thread = threading.Thread(target=lambda: scheduler.run_sprint(roadmap.sprint("s1")))
        thread.start()
        assert reason_b_before_start.wait(timeout=30)
        runner.wait_started("A", timeout=30)
        assert reason_b_after_start.wait(timeout=30)
        assert after_start_dup_seen.wait(timeout=30)
        runner.wait_started("B", timeout=30)
        runner.wait_started("C", timeout=30)
        runner.release()
        thread.join(timeout=120)

    assert not thread.is_alive()
    waits = _wait_messages(caplog)
    assert waits == [
        f"Admission wait: {reason_a}",
        f"Admission wait: {reason_b}",
        f"Admission wait: {reason_b}",
    ]
    assert _printed_waits(printed) == [
        f"Waiting (memory): {reason_a}",
        f"Waiting (memory): {reason_b}",
        f"Waiting (memory): {reason_b}",
    ]
    assert all(state.get(task_id).status == TaskStatus.DONE for task_id in ("A", "B", "C"))
