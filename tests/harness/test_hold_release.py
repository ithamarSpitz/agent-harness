from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

from harness import cli
from harness.models import TaskStatus
from harness.runner import AgentResult
from harness.scheduler import (
    Scheduler,
    hold_request_path,
    read_hold_request,
    read_released_requests,
    update_hold_request,
)
from harness.state import StateStore
from tests.harness.test_scheduler_stop import GatedRunner, _init_repo


def _wait(predicate, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    raise AssertionError("condition not reached")


def _scheduler(tmp_path: Path, ids: tuple[str, ...], runner, *, review: bool = True, **kwargs):
    config, roadmap, state, worktrees, verifier, failures = _init_repo(
        tmp_path, ids, max_parallel=len(ids), review=review, **kwargs
    )
    scheduler = Scheduler(
        tmp_path, config, state, worktrees, runner, verifier, failures=failures, roadmap=roadmap
    )
    return scheduler, roadmap, state


def _hold_then_release(tmp_path: Path, *, fresh_budget: bool) -> Scheduler:
    runner = GatedRunner()
    runner.gate("A", "B")
    scheduler, roadmap, state = _scheduler(tmp_path, ("A", "B"), runner)
    _mark_run_active(state)
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(scheduler.run_sprint(roadmap.sprint("s1"))))
    thread.start()
    runner.wait_started("A")
    runner.wait_started("B")
    hold_path = hold_request_path(tmp_path / ".harness")
    update_hold_request(hold_path, add="A")
    runner.allow("A", "B")

    _wait(
        lambda: state.get("A").status == TaskStatus.BLOCKED
        and state.get("B").status == TaskStatus.PASSED
    )
    assert state.get("A").last_error == "held by operator"
    assert state.get("A").attempt == 1
    assert state.get("A").worktree
    assert thread.is_alive()  # the run waits for the release; nothing else stopped
    assert runner.started_ids.count("B") == 1

    # Exercise the real command through a separate StateStore loaded now, as a
    # separate CLI process would: it sees A as BLOCKED and the run as active,
    # and must leave state.json to the running scheduler.
    _wait_scheduler_idle(scheduler)
    command_state = _command_state(tmp_path)
    assert command_state.get("A").status == TaskStatus.BLOCKED
    runtime = _cli_runtime(tmp_path, scheduler, roadmap, command_state)
    assert cli.cmd_release_task(runtime, "A", fresh_budget=fresh_budget) == 0
    assert command_state.get("A").status == TaskStatus.BLOCKED
    thread.join(timeout=120)
    assert not thread.is_alive()
    assert result == [True]
    assert runner.started_ids.count("A") == 2
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("B").status == TaskStatus.DONE
    assert read_hold_request(hold_path) == []
    return scheduler


def test_hold_blocks_one_task_other_finishes_and_release_reruns(tmp_path: Path):
    _hold_then_release(tmp_path, fresh_budget=False)
    state = json.loads((tmp_path / ".harness" / "state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["A"]["attempt"] == 2


def test_release_fresh_budget_resets_attempts(tmp_path: Path):
    _hold_then_release(tmp_path, fresh_budget=True)
    state = json.loads((tmp_path / ".harness" / "state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["A"]["attempt"] == 1


def test_release_before_in_flight_call_finishes_cancels_hold(tmp_path: Path, caplog):
    caplog.set_level("INFO", logger="harness.scheduler")
    runner = GatedRunner()
    runner.gate("A")
    scheduler, roadmap, state = _scheduler(tmp_path, ("A",), runner)
    _mark_run_active(state)
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(scheduler.run_sprint(roadmap.sprint("s1"))))
    thread.start()
    runner.wait_started("A")

    # Release while the implementation call is still running. The hold is
    # cancelled: the completed call continues normally and fresh-budget is ignored.
    command_state = _command_state(tmp_path)
    runtime = _cli_runtime(tmp_path, scheduler, roadmap, command_state)
    assert cli.cmd_hold(runtime, "A") == 0
    assert cli.cmd_release_task(runtime, "A", fresh_budget=True) == 0
    runner.allow("A")

    thread.join(timeout=120)
    assert not thread.is_alive()
    assert result == [True]
    assert runner.started_ids.count("A") == 1
    assert state.get("A").attempt == 1
    assert state.get("A").status == TaskStatus.DONE
    assert "released before its hold took effect" in caplog.text
    assert "ignoring the fresh-budget request" in caplog.text
    hold_path = hold_request_path(tmp_path / ".harness")
    assert read_hold_request(hold_path) == []
    assert read_released_requests(hold_path) == {}


def test_hold_file_accumulates_ids_and_tolerates_concurrent_writers(tmp_path: Path):
    path = hold_request_path(tmp_path / ".harness")
    ids = [f"T{i}" for i in range(12)]
    threads = [threading.Thread(target=update_hold_request, args=(path,), kwargs={"add": t}) for t in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(read_hold_request(path)) == sorted(ids)
    assert json.loads(path.read_text(encoding="utf-8"))["tasks"]


class FailThenWaitRunner:
    """First attempt fails verification; the stop request lands before attempt two."""

    def __init__(self, on_first_done) -> None:
        self.calls = 0
        self.on_first_done = on_first_done

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.calls += 1
        if self.calls == 1:
            self.on_first_done()
            return AgentResult(True, "ok")  # no output file: verification fails
        (workspace / f"out-{task.id.lower()}.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


def test_stop_request_between_attempts_starts_no_further_attempt(tmp_path: Path):
    stop_path = tmp_path / ".harness" / "stop-request"

    def write_stop() -> None:
        stop_path.parent.mkdir(parents=True, exist_ok=True)
        stop_path.write_text(json.dumps({"sprint": "s1"}) + "\n", encoding="utf-8")

    runner = FailThenWaitRunner(write_stop)
    scheduler, roadmap, state = _scheduler(tmp_path, ("A",), runner)
    assert scheduler.run_sprint(roadmap.sprint("s1")) is False
    assert runner.calls == 1
    assert state.get("A").status == TaskStatus.RETRY
    assert state.get("A").attempt == 1

    assert scheduler.run_sprint(roadmap.sprint("s1")) is True
    assert runner.calls == 2
    assert state.get("A").status == TaskStatus.DONE


def _cli_runtime(tmp_path: Path, scheduler, roadmap, state):
    return SimpleNamespace(root=tmp_path, roadmap=roadmap, state=state, scheduler=scheduler)


def _wait_scheduler_idle(scheduler: Scheduler) -> None:
    """Wait for two more stage-loop passes; a loop waiting on a hold saves nothing.

    Loading a StateStore while the scheduler saves can make its os.replace fail
    on Windows, so the CLI snapshot is loaded only once the scheduler is quiet.
    """
    passes: list[int] = []
    original = scheduler._set_ready_waiting

    def counting(ready_tasks: int) -> None:
        passes.append(ready_tasks)
        original(ready_tasks)

    scheduler._set_ready_waiting = counting  # type: ignore[method-assign]
    try:
        _wait(lambda: len(passes) >= 2)
    finally:
        del scheduler._set_ready_waiting


def _mark_run_active(state: StateStore) -> None:
    """Record an active run for s1, as `run`/`resume` does through the manifest."""
    state.set_meta("s1.active_run_id", "s1-run")


def _command_state(tmp_path: Path) -> StateStore:
    """A CLI process's StateStore that fails the test if it is ever saved."""
    command_state = StateStore(tmp_path / ".harness" / "state.json")

    def refuse_save() -> None:
        raise AssertionError("release rewrote state.json while a scheduler was running")

    command_state.save = refuse_save  # type: ignore[method-assign]
    return command_state


def test_status_shows_held_and_error_cases_change_nothing(tmp_path: Path, capsys):
    scheduler, roadmap, state = _scheduler(tmp_path, ("A", "B"), GatedRunner())
    runtime = _cli_runtime(tmp_path, scheduler, roadmap, state)
    hold_path = hold_request_path(tmp_path / ".harness")

    assert cli.cmd_hold(runtime, "NOPE") == 2
    assert "Unknown task" in capsys.readouterr().out
    assert cli.cmd_release_task(runtime, "A") == 2
    assert "not held" in capsys.readouterr().out
    assert not hold_path.exists()

    assert cli.cmd_hold(runtime, "A") == 0
    capsys.readouterr()
    assert read_hold_request(hold_path) == ["A"]
    before = state.get("A").status
    assert cli.cmd_hold(runtime, "A") == 0
    assert "already held" in capsys.readouterr().out
    assert read_hold_request(hold_path) == ["A"]
    assert state.get("A").status == before

    cli.cmd_status(roadmap, state, "s1", tmp_path / ".harness")
    out = capsys.readouterr().out
    assert any(line.split()[:2] == ["held", "A"] for line in out.splitlines())
    assert not any(line.split()[:2] == ["held", "B"] for line in out.splitlines())

    state.get("A").status = TaskStatus.BLOCKED
    state.get("A").last_error = "held by operator"
    state.get("A").attempt = 2
    assert cli.cmd_release_task(runtime, "A", fresh_budget=True) == 0
    assert read_hold_request(hold_path) == []
    # With no run going, release itself puts the task back to RETRY and resets
    # the budget; the release is applied, so no `released` entry is left.
    assert read_released_requests(hold_path) == {}
    assert state.get("A").status == TaskStatus.RETRY
    assert state.get("A").attempt == 0
    on_disk = StateStore(tmp_path / ".harness" / "state.json")
    assert on_disk.get("A").status == TaskStatus.RETRY


def test_hold_during_call_blocks_task_without_review(tmp_path: Path):
    runner = GatedRunner()
    runner.gate("A")
    scheduler, roadmap, state = _scheduler(tmp_path, ("A",), runner, review=False)
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(scheduler.run_sprint(roadmap.sprint("s1"))))
    thread.start()
    runner.wait_started("A")
    hold_path = hold_request_path(tmp_path / ".harness")
    update_hold_request(hold_path, add="A")
    runner.allow("A")
    _wait(lambda: state.get("A").status == TaskStatus.BLOCKED)
    assert state.get("A").last_error == "held by operator"
    assert state.get("A").commit is None
    update_hold_request(hold_path, remove="A", release_fresh_budget=False)
    thread.join(timeout=120)
    assert result == [True]
    assert state.get("A").status == TaskStatus.DONE


class HoldWithQuotaRunner:
    def __init__(self, hold_path: Path, *, phase: str) -> None:
        self.hold_path = hold_path
        self.phase = phase
        self.implements = 0
        self.reviews = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.implements += 1
        if self.phase == "implement" and self.implements == 1:
            update_hold_request(self.hold_path, add=task.id)
            return AgentResult(
                False, "quota", "quota", quota_exhausted=True, provider="cursor"
            )
        (workspace / f"out-{task.id.lower()}.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok", provider="cursor")

    def review(self, task, *args, **kwargs):
        self.reviews += 1
        if self.phase == "review" and self.reviews == 1:
            update_hold_request(self.hold_path, add=task.id)
            return AgentResult(
                False, "quota", "quota", quota_exhausted=True, provider="cursor"
            )
        return AgentResult(True, "VERDICT: PASS\n", provider="cursor")


def _assert_hold_wins_over_quota(tmp_path: Path, *, phase: str) -> None:
    hold_path = hold_request_path(tmp_path / ".harness")
    runner = HoldWithQuotaRunner(hold_path, phase=phase)
    scheduler, roadmap, state = _scheduler(tmp_path, ("A",), runner)
    _mark_run_active(state)
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(scheduler.run_sprint(roadmap.sprint("s1"))))
    thread.start()
    _wait(lambda: state.get("A").status == TaskStatus.BLOCKED)
    assert state.get("A").last_error == "held by operator"
    assert state.get("A").attempt == 1
    assert thread.is_alive()

    _wait_scheduler_idle(scheduler)
    assert cli.cmd_release_task(
        _cli_runtime(tmp_path, scheduler, roadmap, _command_state(tmp_path)), "A"
    ) == 0
    thread.join(timeout=120)
    assert not thread.is_alive()
    assert result == [True]
    assert state.get("A").status == TaskStatus.DONE


def test_hold_during_implementation_quota_result_blocks_instead_of_waiting(tmp_path: Path):
    _assert_hold_wins_over_quota(tmp_path, phase="implement")


def test_hold_during_review_quota_result_blocks_instead_of_waiting(tmp_path: Path):
    _assert_hold_wins_over_quota(tmp_path, phase="review")


class HoldDuringReviewRunner:
    def __init__(self, hold_path: Path) -> None:
        self.hold_path = hold_path
        self.reviews = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        (workspace / f"out-{task.id.lower()}.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        self.reviews += 1
        if self.reviews == 1:
            update_hold_request(self.hold_path, add="A")
        return AgentResult(True, "VERDICT: PASS\n")


def test_hold_during_review_blocks_instead_of_committing(tmp_path: Path):
    hold_path = hold_request_path(tmp_path / ".harness")
    runner = HoldDuringReviewRunner(hold_path)
    scheduler, roadmap, state = _scheduler(tmp_path, ("A",), runner)
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(scheduler.run_sprint(roadmap.sprint("s1"))))
    thread.start()
    _wait(lambda: state.get("A").status == TaskStatus.BLOCKED)
    assert state.get("A").last_error == "held by operator"
    assert state.get("A").commit is None
    update_hold_request(hold_path, remove="A", release_fresh_budget=False)
    thread.join(timeout=120)
    assert result == [True]
    assert state.get("A").status == TaskStatus.DONE


def test_release_of_held_pending_task_is_applied_on_next_resume(tmp_path: Path):
    scheduler, roadmap, state = _scheduler(tmp_path, ("A",), GatedRunner())
    runtime = _cli_runtime(tmp_path, scheduler, roadmap, state)
    assert state.get("A").status == TaskStatus.PENDING
    assert cli.cmd_hold(runtime, "A") == 0
    assert cli.cmd_release_task(runtime, "A") == 0
    assert state.get("A").status == TaskStatus.RETRY
    assert read_released_requests(hold_request_path(tmp_path / ".harness")) == {}
    assert scheduler.run_sprint(roadmap.sprint("s1")) is True
    assert state.get("A").status == TaskStatus.DONE
    assert read_released_requests(hold_request_path(tmp_path / ".harness")) == {}


def test_release_with_stale_active_run_updates_state_directly(tmp_path: Path):
    # A stopped run keeps its active run id, but no live scheduler owns state.
    scheduler, roadmap, state = _scheduler(tmp_path, ("A",), GatedRunner())
    _mark_run_active(state)
    runtime_a = state.get("A")
    runtime_a.status = TaskStatus.BLOCKED
    runtime_a.last_error = "held by operator"
    runtime_a.attempt = 2
    state.save()
    hold_path = hold_request_path(tmp_path / ".harness")
    update_hold_request(hold_path, add="A")

    command_state = StateStore(tmp_path / ".harness" / "state.json")
    runtime = _cli_runtime(tmp_path, scheduler, roadmap, command_state)
    assert cli.cmd_release_task(runtime, "A", fresh_budget=True) == 0
    assert read_hold_request(hold_path) == []
    assert read_released_requests(hold_path) == {}
    released = StateStore(tmp_path / ".harness" / "state.json").get("A")
    assert released.status == TaskStatus.RETRY
    assert released.attempt == 0

    state.load()  # a later `resume` builds/reloads scheduler state from disk
    assert scheduler.run_sprint(roadmap.sprint("s1")) is True
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("A").attempt == 1
    assert read_released_requests(hold_path) == {}


def test_hold_refuses_passed_task_and_run_task_does_not_report_stopped_as_passed(
    tmp_path: Path, capsys
):
    scheduler, roadmap, state = _scheduler(tmp_path, ("A",), GatedRunner())
    runtime = _cli_runtime(tmp_path, scheduler, roadmap, state)
    state.get("A").status = TaskStatus.PASSED
    assert cli.cmd_hold(runtime, "A") == 2
    assert read_hold_request(hold_request_path(tmp_path / ".harness")) == []
    state.get("A").status = TaskStatus.RETRY

    stop_path = tmp_path / ".harness" / "stop-request"
    stop_path.parent.mkdir(parents=True, exist_ok=True)
    stop_path.write_text(json.dumps({"sprint": "s1"}) + "\n", encoding="utf-8")
    task = roadmap.task("A")
    result = scheduler._run_task(task, "HEAD")
    assert result.stopped
    assert state.get("A").status == TaskStatus.RETRY
    assert state.get("A").attempt == 0
