from __future__ import annotations

import io
import threading
import time
from collections import deque
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
    should_use_console,
    sprint_run_finished,
)
from harness.console_input import KeyReader, LineEditor
from harness.console_screen import PROMPT_PREFIX
from harness.events import append_event
from harness.headroom import Headroom
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


class FakeStdin:
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


def test_should_use_console_requires_stdin_and_stdout_tty() -> None:
    assert (
        should_use_console(FakeTTY(), io.StringIO(), ansi_enabled=lambda: True) is False
    )
    assert (
        should_use_console(io.StringIO(), FakeStdin(), ansi_enabled=lambda: True)
        is False
    )
    assert (
        should_use_console(FakeTTY(), FakeStdin(), no_board=True, ansi_enabled=lambda: True)
        is False
    )
    assert should_use_console(FakeTTY(), FakeStdin(), ansi_enabled=lambda: True) is True


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


def test_board_loop_passes_live_headroom_and_configured_provider_order(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    runtime.config.providers = SimpleNamespace(priority=["cursor", "codex"])
    calls: list[int] = []

    def fake_headroom() -> dict[str, list[Headroom]]:
        calls.append(1)
        return {
            "codex": [Headroom("codex", 96, None, "five_hour", True)],
            "cursor": [Headroom("cursor", 25, None, "other_models", True)],
        }

    loop = BoardLoop(
        runtime,
        "s1",
        stream=FakeTTY(),
        free_memory=lambda: 4.0,
        headroom_source=fake_headroom,
    )

    snapshot = loop.build_snapshot()

    assert calls == [1]
    assert snapshot.headroom is not None
    assert list(snapshot.headroom) == ["cursor", "codex"]


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


def _fake_posix_reader(
    keys: deque[str] | list[list[str]],
    calls: list[object] | None = None,
) -> KeyReader:
    """Build a KeyReader whose poll() returns staged key batches.

    Pass a deque to drain everything on the first poll, or a list of batches to
    release one batch per poll (needed when a command then asks for confirm).
    """
    if isinstance(keys, deque):
        stages: list[list[str]] = [list(keys)]
        keys.clear()
    else:
        stages = [list(batch) for batch in keys]
    index = {"i": 0}

    def poll_keys() -> list[str]:
        i = index["i"]
        if i >= len(stages):
            return []
        index["i"] = i + 1
        return list(stages[i])

    reader = KeyReader(
        platform="linux",
        fileno=9,
        read=lambda _fd, _size: b"",
        select=lambda *_args: ([], [], []),
        tcgetattr=lambda fd: (calls.append(("get", fd)) if calls is not None else None)
        or ["original"],
        tcsetattr=lambda fd, when, mode: (
            calls.append(("restore", fd, when, mode)) if calls is not None else None
        ),
        setcbreak=lambda fd: calls.append(("cbreak", fd)) if calls is not None else None,
        restore_when=42,
    )
    reader.poll = poll_keys  # type: ignore[method-assign]
    return reader


def test_interactive_board_runs_typed_command_and_keeps_partial_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Command output appears; partial text typed between board redraws is kept."""
    runtime = _runtime(tmp_path)
    state = runtime.state
    state.set_meta("s1.active_run_id", "run-1")
    state.get("T1").status = TaskStatus.DONE
    state.get("T1").attempt = 1
    state.save()

    monkeypatch.setattr("harness.terminal.shutil.get_terminal_size", lambda: _Size())
    stream = FakeTTY()
    # Interleave key batches with empty polls so board-only redraw ticks run
    # while partial input is already in the editor (AC2).
    batches = [
        list("task T1") + ["ENTER"],
        [],
        list("he"),
        [],  # board redraw with partial "he" still in the editor
        list("l"),
        [],
    ]
    ticks = {"n": 0}
    editor = LineEditor()
    saw_partial_across_redraw = {"ok": False}
    partial_stream_len = {"n": -1}

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        # Advance real monotonic time so interval board ticks fire between polls.
        time.sleep(0.015)
        # After "he" is first drawn, a later board-only redraw must still show it.
        out = stream.getvalue()
        if editor.text == "he" and f"{PROMPT_PREFIX}he" in out:
            if partial_stream_len["n"] < 0:
                partial_stream_len["n"] = len(out)
            elif len(out) > partial_stream_len["n"]:
                saw_partial_across_redraw["ok"] = True
        if ticks["n"] >= 12:
            loop.stop()

    loop = BoardLoop(
        runtime,
        "s1",
        stream=stream,
        interval=0.01,
        poll_interval=0.01,
        free_memory=lambda: 4.0,
        now=lambda: datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
        sleep=fake_sleep,
        enable_ansi_fn=lambda: True,
        environment={"NO_COLOR": ""},
        interactive=True,
        mode="watch",
        key_reader_factory=lambda: _fake_posix_reader(batches),
        editor=editor,
    )
    loop.run_until_stopped()
    output = stream.getvalue()
    assert "task: T1" in output
    assert "status: DONE" in output
    assert saw_partial_across_redraw["ok"]
    assert f"{PROMPT_PREFIX}hel" in output
    assert editor.text == "hel"


def test_interactive_quit_and_exception_restore_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    runtime.state.set_meta("s1.active_run_id", "run-1")
    runtime.state.save()
    monkeypatch.setattr("harness.terminal.shutil.get_terminal_size", lambda: _Size())

    calls: list[object] = []
    typed = deque(list("quit") + ["ENTER"])

    def fake_sleep(_seconds: float) -> None:
        if not typed and not loop._stop.is_set():
            # Should have stopped after quit; force-stop if not.
            loop.stop()

    loop = BoardLoop(
        runtime,
        "s1",
        stream=FakeTTY(),
        interval=10.0,
        poll_interval=0.01,
        free_memory=lambda: 4.0,
        sleep=fake_sleep,
        enable_ansi_fn=lambda: True,
        environment={"NO_COLOR": ""},
        interactive=True,
        mode="watch",
        key_reader_factory=lambda: _fake_posix_reader(typed, calls),
    )
    loop.run_until_stopped()
    assert ("restore", 9, 42, ["original"]) in calls

    calls.clear()

    def boom_factory() -> KeyReader:
        reader = _fake_posix_reader(deque(["x"]), calls)

        def poll() -> list[str]:
            raise RuntimeError("boom")

        reader.poll = poll  # type: ignore[method-assign]
        return reader

    broken = BoardLoop(
        runtime,
        "s1",
        stream=FakeTTY(),
        interval=10.0,
        poll_interval=0.01,
        free_memory=lambda: 4.0,
        sleep=lambda _s: broken.stop(),
        enable_ansi_fn=lambda: True,
        environment={"NO_COLOR": ""},
        interactive=True,
        mode="watch",
        key_reader_factory=boom_factory,
    )
    with pytest.raises(RuntimeError, match="boom"):
        broken.run_until_stopped()
    assert ("restore", 9, 42, ["original"]) in calls


def test_interactive_run_quit_confirms_then_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Confirm answer in the same poll batch as quit+Enter must not be stranded."""
    runtime = _runtime(tmp_path)
    runtime.state.set_meta("s1.active_run_id", "run-1")
    runtime.state.save()
    monkeypatch.setattr("harness.terminal.shutil.get_terminal_size", lambda: _Size())

    # Paste / fast burst: quit, Enter, and y arrive in one poll().
    batches = [list("quit") + ["ENTER", "y"]]
    ticks = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 8:
            loop.stop()

    loop = BoardLoop(
        runtime,
        "s1",
        stream=FakeTTY(),
        interval=10.0,
        poll_interval=0.01,
        free_memory=lambda: 4.0,
        now=lambda: datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
        sleep=fake_sleep,
        enable_ansi_fn=lambda: True,
        environment={"NO_COLOR": ""},
        interactive=True,
        mode="run",
        key_reader_factory=lambda: _fake_posix_reader(batches),
    )
    loop.run_until_stopped()
    stop_path = tmp_path / ".harness" / "stop-request"
    assert stop_path.is_file()
    assert "s1" in stop_path.read_text(encoding="utf-8")
    assert loop._output_lines == ["Stop requested for s1."]


def test_interactive_run_ctrl_c_during_confirm_interrupts_main(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl+C at confirm must call interrupt_main (Decision 3), not only kill the board thread."""
    runtime = _runtime(tmp_path)
    runtime.state.set_meta("s1.active_run_id", "run-1")
    runtime.state.save()
    monkeypatch.setattr("harness.terminal.shutil.get_terminal_size", lambda: _Size())

    interrupted = {"n": 0}

    def fake_interrupt_main() -> None:
        interrupted["n"] += 1

    monkeypatch.setattr(
        "harness.board_loop._thread.interrupt_main", fake_interrupt_main
    )

    # quit+Enter opens confirm; CTRL_C arrives on the next poll.
    batches = [list("quit") + ["ENTER"], ["CTRL_C"]]
    ticks = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 8:
            loop.stop()

    loop = BoardLoop(
        runtime,
        "s1",
        stream=FakeTTY(),
        interval=10.0,
        poll_interval=0.01,
        free_memory=lambda: 4.0,
        now=lambda: datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
        sleep=fake_sleep,
        enable_ansi_fn=lambda: True,
        environment={"NO_COLOR": ""},
        interactive=True,
        mode="run",
        key_reader_factory=lambda: _fake_posix_reader(batches),
    )
    loop.run_until_stopped()
    assert interrupted["n"] == 1
    assert loop._stop.is_set()
    assert not (tmp_path / ".harness" / "stop-request").is_file()


def test_close_restores_terminal_when_board_thread_stuck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """close() must restore cbreak even if join times out on a stuck thread."""
    runtime = _runtime(tmp_path)
    runtime.state.set_meta("s1.active_run_id", "run-1")
    runtime.state.save()
    monkeypatch.setattr("harness.terminal.shutil.get_terminal_size", lambda: _Size())

    calls: list[object] = []
    entered = threading.Event()
    release = threading.Event()

    def hang_sleep(_seconds: float) -> None:
        entered.set()
        # Ignore stop so join times out while KeyReader is still in cbreak.
        release.wait(30)

    loop = BoardLoop(
        runtime,
        "s1",
        stream=FakeTTY(),
        interval=0.05,
        poll_interval=0.01,
        free_memory=lambda: 4.0,
        sleep=hang_sleep,
        enable_ansi_fn=lambda: True,
        environment={"NO_COLOR": ""},
        interactive=True,
        mode="watch",
        key_reader_factory=lambda: _fake_posix_reader(deque(), calls),
    )
    loop.start_background()
    assert entered.wait(2.0)
    loop.close()
    assert ("restore", 9, 42, ["original"]) in calls
    release.set()
    loop.join(timeout=2.0)


def test_non_interactive_board_output_unchanged_shape(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without interactive mode the drawn screen is still board-only (no prompt)."""
    runtime = _runtime(tmp_path)
    runtime.state.set_meta("s1.active_run_id", "run-1")
    runtime.state.get("T1").status = TaskStatus.PENDING
    runtime.state.save()
    monkeypatch.setattr("harness.terminal.shutil.get_terminal_size", lambda: _Size())
    stream = FakeTTY()

    def fake_sleep(_seconds: float) -> None:
        loop.stop()

    loop = BoardLoop(
        runtime,
        "s1",
        stream=stream,
        interval=0.05,
        poll_interval=0.01,
        free_memory=lambda: 4.0,
        sleep=fake_sleep,
        enable_ansi_fn=lambda: True,
        environment={"NO_COLOR": ""},
        interactive=False,
    )
    loop.run_until_stopped()
    assert PROMPT_PREFIX not in stream.getvalue()
    assert "s1" in stream.getvalue()
