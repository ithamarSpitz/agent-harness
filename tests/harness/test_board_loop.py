from __future__ import annotations

import io
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.board import BoardSnapshot, BoardStage, BoardTask
from harness.board_loop import (
    BoardLoop,
    colorize_board_lines,
    count_running_agents,
    find_active_sprint_id,
    format_parallel_summary,
    should_use_board,
    sprint_run_finished,
)
from harness.events import append_event
from harness.models import SprintSpec, TaskSpec, TaskStatus
from harness.state import StateStore


class FakeTTY(io.StringIO):
    def __init__(self, encoding: str = "utf-8") -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:
        return self._encoding

    def isatty(self) -> bool:
        return True


class _Size:
    columns = 100
    lines = 40


def _sprint() -> SprintSpec:
    return SprintSpec(
        id="s1",
        name="Demo",
        tasks={
            "T1": TaskSpec(id="T1", file=Path("t1.md"), sprint="s1", stage=1),
            "T2": TaskSpec(
                id="T2",
                file=Path("t2.md"),
                sprint="s1",
                stage=1,
                depends_on=["T1"],
                model_class="worker",
            ),
        },
    )


def _runtime(tmp_path: Path, sprint: SprintSpec | None = None) -> SimpleNamespace:
    sprint = sprint or _sprint()
    state = StateStore(tmp_path / ".harness" / "state.json")
    config = SimpleNamespace(
        execution=SimpleNamespace(
            max_parallel_agents=3,
            memory_per_agent_gb=1.5,
            default_max_attempts=2,
        )
    )
    roadmap = SimpleNamespace(
        sprints={"s1": sprint},
        sprint=lambda sprint_id: sprint
        if sprint_id == sprint.id
        else (_ for _ in ()).throw(KeyError(sprint_id)),
    )
    return SimpleNamespace(
        root=tmp_path,
        state=state,
        roadmap=roadmap,
        config=config,
        scheduler=SimpleNamespace(
            task_queue=SimpleNamespace(merge_added_tasks_from_state=lambda _s: None)
        ),
    )


def test_should_use_board_respects_tty_ansi_and_flag() -> None:
    assert should_use_board(io.StringIO(), ansi_enabled=lambda: True) is False
    assert should_use_board(FakeTTY(), no_board=True, ansi_enabled=lambda: True) is False
    assert should_use_board(FakeTTY(), ansi_enabled=lambda: False) is False
    assert should_use_board(FakeTTY(), ansi_enabled=lambda: True) is True


def test_format_parallel_summary() -> None:
    assert format_parallel_summary(running=1, maximum=3) == "parallel 1/3"
    assert (
        format_parallel_summary(running=2, maximum=3, free_gb=10.5, per_agent_gb=4.0)
        == "parallel 2/3 (free 10.5 GB, ~4 GB/agent)"
    )


def test_colorize_respects_no_color() -> None:
    snapshot = BoardSnapshot(
        sprint_id="s1",
        sprint_title="Demo",
        current_stage=1,
        stage_count=1,
        stages=[
            BoardStage(
                number=1,
                tasks=[BoardTask(id="T1", status="DONE")],
            )
        ],
    )
    lines = ["  T1 DONE try 1/2"]
    assert colorize_board_lines(snapshot, lines, environment={"NO_COLOR": ""}) == lines
    colored = colorize_board_lines(snapshot, lines, environment={})
    assert "\x1b[32mDONE\x1b[0m" in colored[0]


def test_board_loop_draws_from_state_and_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    sprint = runtime.roadmap.sprint("s1")
    state = runtime.state
    state.set_meta("s1.active_run_id", "run-1")
    state.get("T1").status = TaskStatus.DONE
    state.get("T1").attempt = 1
    running = state.get("T2")
    running.status = TaskStatus.IMPLEMENTING
    running.attempt = 1
    state.set_meta("s1.T2.board.provider", "cursor")
    state.set_meta("s1.T2.board.model", "worker")
    state.save()
    append_event(tmp_path, "s1", "T2 IMPLEMENTING")

    stream = FakeTTY()
    monkeypatch.setattr("harness.terminal.shutil.get_terminal_size", lambda: _Size())
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        loop.stop()

    loop = BoardLoop(
        runtime,
        "s1",
        stream=stream,
        interval=0.05,
        poll_interval=0.01,
        reload_from_disk=True,
        stop_when_finished=False,
        free_memory=lambda: 8.0,
        now=lambda: datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
        sleep=fake_sleep,
        enable_ansi_fn=lambda: True,
        environment={},
    )
    loop.run_until_stopped()
    output = stream.getvalue()
    assert "s1   Demo   stage" in output
    assert "parallel 1/3" in output
    assert "T2 IMPLEMENTING" in output
    assert count_running_agents(state, sprint) == 1
    assert find_active_sprint_id(runtime.roadmap, state) == "s1"
    assert sleeps


def test_board_loop_stops_when_sprint_finished(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    state = runtime.state
    state.get("T1").status = TaskStatus.DONE
    state.get("T2").status = TaskStatus.DONE
    state.set_meta("s1.run_status", "COMPLETED")
    state.save()

    monkeypatch.setattr("harness.terminal.shutil.get_terminal_size", lambda: _Size())
    stream = FakeTTY()
    loop = BoardLoop(
        runtime,
        "s1",
        stream=stream,
        interval=0.05,
        poll_interval=0.01,
        reload_from_disk=True,
        stop_when_finished=True,
        free_memory=lambda: 4.0,
        sleep=lambda _s: None,
        enable_ansi_fn=lambda: True,
        environment={"NO_COLOR": ""},
    )
    loop.run_until_stopped()
    assert "s1   Demo" in stream.getvalue()
    assert sprint_run_finished(state, runtime.roadmap.sprint("s1")) is True


def test_board_loop_redraws_on_state_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    state = runtime.state
    state.set_meta("s1.active_run_id", "run-1")
    state.get("T1").status = TaskStatus.PENDING
    state.save()

    monkeypatch.setattr("harness.terminal.shutil.get_terminal_size", lambda: _Size())
    stream = FakeTTY()
    ticks = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] == 1:
            state.get("T1").status = TaskStatus.IMPLEMENTING
            state.save()
        elif ticks["n"] >= 3:
            loop.stop()

    loop = BoardLoop(
        runtime,
        "s1",
        stream=stream,
        interval=10.0,
        poll_interval=0.01,
        reload_from_disk=True,
        free_memory=lambda: 5.0,
        sleep=fake_sleep,
        enable_ansi_fn=lambda: True,
        environment={"NO_COLOR": ""},
    )
    loop.run_until_stopped()
    assert "IMPLEMENTING" in stream.getvalue()


def test_board_loop_does_not_mutate_live_sprint_tasks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Board must merge S7 added tasks on a private copy, not the live map."""
    sprint = _sprint()
    live_ids = set(sprint.tasks)
    added = TaskSpec(id="T-ADD", file=Path("t-add.md"), sprint="s1", stage=1)

    def merge_added(target: SprintSpec) -> None:
        if added.id not in target.tasks:
            target.tasks[added.id] = added

    runtime = _runtime(tmp_path, sprint)
    runtime.scheduler = SimpleNamespace(
        task_queue=SimpleNamespace(merge_added_tasks_from_state=merge_added)
    )
    state = runtime.state
    state.set_meta("s1.active_run_id", "run-1")
    state.get("T1").status = TaskStatus.DONE
    state.get("T-ADD").status = TaskStatus.PENDING
    state.get("T-ADD").added = True
    state.save()

    monkeypatch.setattr("harness.terminal.shutil.get_terminal_size", lambda: _Size())
    loop = BoardLoop(
        runtime,
        "s1",
        stream=FakeTTY(),
        free_memory=lambda: 4.0,
        now=lambda: datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
        enable_ansi_fn=lambda: True,
        environment={"NO_COLOR": ""},
    )
    snapshot = loop.build_snapshot()
    assert set(sprint.tasks) == live_ids
    assert added.id not in sprint.tasks
    assert any(task.id == added.id for stage in snapshot.stages for task in stage.tasks)
