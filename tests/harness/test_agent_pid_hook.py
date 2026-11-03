from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from harness.agent_ledger import LedgerStore
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.models import TaskStatus
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier
from tests.harness.test_scheduler_memory import FakeSampler, init_repo


def _scheduler(
    root: Path,
    state: StateStore,
    worktrees: WorktreeManager,
    runner,
    verifier: Verifier,
    failures: FailureStore,
    *,
    sampler_factory,
    config,
) -> Scheduler:
    return Scheduler(
        root,
        config,
        state,
        worktrees,
        runner,
        verifier,
        failures=failures,
        free_memory_gb=lambda: 50.0,
        ledger=LedgerStore(
            root / ".harness" / "test-ledger",
            process_alive=lambda _pid: True,
        ),
        sampler_factory=sampler_factory,
    )


def test_subprocess_popen_remains_original_class_during_agent_call(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(tmp_path, ("A",))
    real_popen = subprocess.Popen
    seen: list[object] = []

    class HookRunner:
        def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
            seen.append(subprocess.Popen)
            assert subprocess.Popen is real_popen
            on_process_start = kwargs.get("on_process_start")
            proc = subprocess.Popen(
                [sys.executable, "-c", "pass"],
                cwd=workspace,
            )
            if on_process_start is not None:
                on_process_start(proc.pid)
            assert proc.wait(timeout=5) == 0
            (workspace / "out-a.txt").write_text("ok\n", encoding="utf-8")
            return AgentResult(True, "ok")

        def review(self, *args, **kwargs):
            return AgentResult(True, "VERDICT: PASS\n")

    scheduler = _scheduler(
        tmp_path,
        state,
        worktrees,
        HookRunner(),
        verifier,
        failures,
        config=config,
        sampler_factory=lambda _pid, _interval: FakeSampler(0.0),
    )

    assert subprocess.Popen is real_popen
    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert subprocess.Popen is real_popen
    assert seen == [real_popen]
    assert state.get("A").status == TaskStatus.DONE


def test_concurrent_agent_calls_sample_only_their_own_pid(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = init_repo(
        tmp_path, ("A", "B"), max_parallel=2
    )
    real_popen = subprocess.Popen
    agent_pids: dict[str, int] = {}
    factory_pids_by_task: dict[str, list[int]] = {"A": [], "B": []}
    thread_task: dict[int, str] = {}
    samples_lock = threading.Lock()
    started = {"A": threading.Event(), "B": threading.Event()}
    release = {"A": threading.Event(), "B": threading.Event()}

    class IsolatedRunner:
        def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
            assert subprocess.Popen is real_popen
            on_process_start = kwargs.get("on_process_start")
            thread_task[threading.get_ident()] = task.id

            proc = real_popen(
                [sys.executable, "-c", "import time; time.sleep(0.3)"],
                cwd=workspace,
            )
            with samples_lock:
                agent_pids[task.id] = proc.pid
            if on_process_start is not None:
                on_process_start(proc.pid)
            started[task.id].set()
            for event in started.values():
                assert event.wait(timeout=5)
            release[task.id].set()
            for event in release.values():
                assert event.wait(timeout=5)
            assert proc.wait(timeout=5) == 0
            (workspace / f"out-{task.id.lower()}.txt").write_text("ok\n", encoding="utf-8")
            return AgentResult(True, "ok")

        def review(self, *args, **kwargs):
            return AgentResult(True, "VERDICT: PASS\n")

    def recording_factory(pid: int, _interval: float):
        task_id = thread_task.get(threading.get_ident())
        if task_id is not None:
            with samples_lock:
                factory_pids_by_task[task_id].append(pid)
        return FakeSampler(0.0)

    scheduler = _scheduler(
        tmp_path,
        state,
        worktrees,
        IsolatedRunner(),
        verifier,
        failures,
        config=config,
        sampler_factory=recording_factory,
    )

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert subprocess.Popen is real_popen
    assert set(agent_pids) == {"A", "B"}
    assert agent_pids["A"] != agent_pids["B"]
    # Each call's sampler_factory must see that call's agent PID and must not
    # see the other concurrent call's agent PID (verification PIDs on the same
    # thread may also appear).
    assert agent_pids["A"] in factory_pids_by_task["A"]
    assert agent_pids["B"] not in factory_pids_by_task["A"]
    assert agent_pids["B"] in factory_pids_by_task["B"]
    assert agent_pids["A"] not in factory_pids_by_task["B"]
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("B").status == TaskStatus.DONE


def test_concurrent_verification_hooks_stay_isolated(tmp_path: Path):
    """Two overlapping verify_task calls on one Verifier keep PIDs isolated."""
    config, roadmap, _state, _worktrees, verifier, _failures = init_repo(
        tmp_path, ("A", "B"), max_parallel=2
    )
    hooks_seen: dict[str, list[int]] = {"A": [], "B": []}
    hooks_lock = threading.Lock()
    barrier = threading.Barrier(2, timeout=5)
    before_attrs = dict(verifier.__dict__)

    task_a = roadmap.sprint("s1").tasks["A"]
    task_b = roadmap.sprint("s1").tasks["B"]
    # Slow command so both threads hold the barrier while processes are live.
    sleep_command = f'{sys.executable} -c "import time; time.sleep(0.3)"'
    task_a.verification = [sleep_command]
    task_b.verification = [sleep_command]
    config.verification.global_task_commands = []
    worktree = tmp_path

    def make_hook(task_id: str):
        def on_process_start(pid: int) -> None:
            with hooks_lock:
                hooks_seen[task_id].append(pid)
            barrier.wait()

        return on_process_start

    errors: list[BaseException] = []

    def run_one(task) -> None:
        try:
            verifier.verify_task(
                task,
                worktree,
                "HEAD",
                env={},
                on_process_start=make_hook(task.id),
            )
        except BaseException as exc:  # noqa: BLE001 — collect for main thread
            errors.append(exc)

    threads = [
        threading.Thread(target=run_one, args=(task_a,)),
        threading.Thread(target=run_one, args=(task_b,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
        assert not thread.is_alive()

    assert errors == []
    # global_task_commands was cleared; each task has one sleep command.
    assert len(hooks_seen["A"]) == 1
    assert len(hooks_seen["B"]) == 1
    assert hooks_seen["A"][0] != hooks_seen["B"][0]
    # Shared Verifier must not gain or change instance attributes from the hooks.
    assert dict(verifier.__dict__) == before_attrs
