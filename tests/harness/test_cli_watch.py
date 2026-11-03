from __future__ import annotations

import io
from pathlib import Path
from types import SimpleNamespace

import pytest

import harness.cli as cli
from harness.board_loop import should_use_board, suppress_progress_stdout
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


def _fake_keep_awake(monkeypatch: pytest.MonkeyPatch, events: list[object]) -> None:
    class FakeKeepAwake:
        def __init__(self, enabled: bool):
            events.append(("init", enabled))

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append("exit")

    monkeypatch.setattr(cli, "KeepAwake", FakeKeepAwake)


def _sprint() -> SprintSpec:
    return SprintSpec(
        id="s1",
        name="Demo",
        tasks={
            "T1": TaskSpec(id="T1", file=Path("t1.md"), sprint="s1", stage=1),
        },
    )


def _runtime_for_watch(tmp_path: Path) -> SimpleNamespace:
    sprint = _sprint()
    state = StateStore(tmp_path / ".harness" / "state.json")
    state.set_meta("s1.active_run_id", "run-1")
    task = state.get("T1")
    task.status = TaskStatus.IMPLEMENTING
    task.attempt = 1
    state.save()
    return SimpleNamespace(
        root=tmp_path,
        state=state,
        roadmap=SimpleNamespace(
            sprints={"s1": sprint},
            sprint=lambda sprint_id: sprint,
        ),
        config=SimpleNamespace(
            execution=SimpleNamespace(
                keep_awake_during_run=True,
                max_parallel_agents=3,
                memory_per_agent_gb=2.0,
                default_max_attempts=2,
            )
        ),
        scheduler=SimpleNamespace(
            task_queue=SimpleNamespace(merge_added_tasks_from_state=lambda _s: None)
        ),
    )


@pytest.mark.parametrize("enabled", [True, False])
def test_watch_cli_holds_keep_awake_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, enabled: bool
) -> None:
    events: list[object] = []
    _fake_keep_awake(monkeypatch, events)
    runtime = SimpleNamespace(
        config=SimpleNamespace(
            execution=SimpleNamespace(keep_awake_during_run=enabled),
        )
    )
    monkeypatch.setattr(cli, "build_runtime", lambda root, roadmap, config: runtime)

    def watch_live(runtime_arg, sprint_id):
        assert runtime_arg is runtime
        assert sprint_id == "s1"
        assert events == [("init", enabled), "enter"]
        return 17

    monkeypatch.setattr(cli, "_cmd_watch_live", watch_live)
    assert cli.main(["--root", str(tmp_path), "watch", "s1"]) == 17
    assert events == [("init", enabled), "enter", "exit"]


def test_watch_draws_board_and_leaves_state_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime_for_watch(tmp_path)
    state_path = tmp_path / ".harness" / "state.json"
    before = state_path.read_bytes()

    monkeypatch.setattr("harness.terminal.shutil.get_terminal_size", lambda: _Size())
    monkeypatch.setattr(cli.sys, "stdout", FakeTTY())

    ticks = {"n": 0}

    def fake_sleep(_seconds: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 2:
            raise KeyboardInterrupt

    monkeypatch.setattr("harness.board_loop.time.sleep", fake_sleep)
    monkeypatch.setattr("harness.board_loop.enable_ansi", lambda: True)
    monkeypatch.setattr("harness.board_loop.default_free_memory_gb", lambda: 9.0)

    assert cli._cmd_watch_live(runtime, "s1") == 0
    after = state_path.read_bytes()
    assert after == before
    output = cli.sys.stdout.getvalue()
    assert "s1   Demo" in output
    assert "IMPLEMENTING" in output
    assert "parallel 1/3" in output


def test_watch_defaults_to_active_sprint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime_for_watch(tmp_path)
    seen: list[str] = []

    class FakeLoop:
        def __init__(self, runtime_arg, sprint_id, **kwargs):
            seen.append(sprint_id)
            self.runtime = runtime_arg

        def run_until_stopped(self) -> None:
            return None

        def close(self) -> None:
            return None

        def stop(self) -> None:
            return None

    monkeypatch.setattr(cli, "BoardLoop", FakeLoop)
    assert cli._cmd_watch_live(runtime, None) == 0
    assert seen == ["s1"]


def _fake_run_runtime(
    tmp_path: Path, *, run_sprint
) -> tuple[SprintSpec, SimpleNamespace]:
    sprint = _sprint()
    state = StateStore(tmp_path / ".harness" / "state.json")
    state.get("T1").status = TaskStatus.PENDING
    state.save()
    (tmp_path / ".harness" / "runs").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".harness" / "runs" / "m.json").write_text("{}", encoding="utf-8")
    runtime = SimpleNamespace(
        root=tmp_path,
        roadmap_path=tmp_path / "tasks" / "roadmap.yaml",
        config_path=tmp_path / "harness.yaml",
        roadmap=SimpleNamespace(sprint=lambda _sid: sprint),
        state=state,
        config=SimpleNamespace(
            execution=SimpleNamespace(keep_awake_during_run=False),
            quota=SimpleNamespace(policy="pause"),
            capacity=SimpleNamespace(policy="pause"),
        ),
        scheduler=SimpleNamespace(
            task_queue=SimpleNamespace(merge_added_tasks_from_state=lambda _s: None),
            run_sprint=run_sprint,
            finalize_completed_sprint=lambda _s: None,
            _pickup_queue=lambda *a, **k: None,
        ),
        worktrees=SimpleNamespace(
            current_branch=lambda: "main",
        ),
        manifests=SimpleNamespace(
            start_or_resume=lambda **kwargs: tmp_path / ".harness" / "runs" / "m.json",
            finish=lambda *a, **k: None,
            set_status=lambda *a, **k: None,
        ),
        failures=SimpleNamespace(get=lambda _sid: None),
    )
    return sprint, runtime


def test_no_board_flag_keeps_plain_progress_on_fake_tty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake_out = FakeTTY()
    monkeypatch.setattr(cli.sys, "stdout", fake_out)
    assert should_use_board(fake_out, no_board=False, ansi_enabled=lambda: True) is True
    assert should_use_board(fake_out, no_board=True, ansi_enabled=lambda: True) is False

    printed: list[str] = []

    def run_sprint(sprint, *, dry_run=False):
        print(f"Stage 1: {next(iter(sprint.tasks))}")
        printed.append("progress")
        return True

    sprint, runtime = _fake_run_runtime(tmp_path, run_sprint=run_sprint)
    monkeypatch.setattr(cli, "_run_pending_revalidations", lambda *_a, **_k: True)
    monkeypatch.setattr(cli, "_prepare_sprint_base", lambda *_a, **_k: None)

    code = cli._cmd_run_live(runtime, "s1", sprint, no_board=True)
    assert code == 0
    assert printed == ["progress"]
    assert "Stage 1: T1" in fake_out.getvalue()


def test_non_tty_run_output_byte_identical_to_plain_baseline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """AC1: Non-TTY run output is byte-identical to plain (pre-board) lines."""

    def run_sprint(sprint, *, dry_run=False):
        print(f"Stage 1: {next(iter(sprint.tasks))}")
        print("task T1 DONE")
        return True

    def capture(root: Path, *, no_board: bool) -> bytes:
        out = io.StringIO()
        assert getattr(out, "isatty")() is False
        monkeypatch.setattr(cli.sys, "stdout", out)
        sprint, runtime = _fake_run_runtime(root, run_sprint=run_sprint)
        monkeypatch.setattr(cli, "_run_pending_revalidations", lambda *_a, **_k: True)
        monkeypatch.setattr(cli, "_prepare_sprint_base", lambda *_a, **_k: None)
        board_starts: list[str] = []

        class TrackingBoard:
            def __init__(self, *args, **kwargs):
                board_starts.append("constructed")

            def start_background(self):
                board_starts.append("started")
                return self

            def close(self) -> None:
                return None

        monkeypatch.setattr(cli, "BoardLoop", TrackingBoard)
        code = cli._cmd_run_live(runtime, "s1", sprint, no_board=no_board)
        assert code == 0
        assert board_starts == []
        return out.getvalue().encode("utf-8")

    baseline_root = tmp_path / "baseline"
    actual_root = tmp_path / "actual"
    baseline_root.mkdir()
    actual_root.mkdir()
    # Explicit plain mode is the pre-board baseline; non-TTY default must match.
    baseline = capture(baseline_root, no_board=True)
    actual = capture(actual_root, no_board=False)
    assert actual == baseline
    assert b"Stage 1: T1" in actual
    assert b"task T1 DONE" in actual
    assert b"Run manifest:" in actual


def test_board_mode_suppresses_progress_prints(monkeypatch: pytest.MonkeyPatch) -> None:
    real = FakeTTY()
    monkeypatch.setattr(cli.sys, "stdout", real)
    with suppress_progress_stdout(real):
        print("hidden progress")
        real.write("board line\n")
    assert "hidden progress" not in real.getvalue()
    assert "board line" in real.getvalue()


def test_status_prints_parallel_summary_while_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    sprint = _sprint()
    state = StateStore(tmp_path / ".harness" / "state.json")
    state.set_meta("s1.active_run_id", "run-1")
    state.get("T1").status = TaskStatus.IMPLEMENTING
    state.save()
    roadmap = SimpleNamespace(
        root=tmp_path,
        sprints={"s1": sprint},
        sprint=lambda sid: sprint,
    )
    config = SimpleNamespace(
        execution=SimpleNamespace(max_parallel_agents=3, memory_per_agent_gb=2.5)
    )
    monkeypatch.setattr(cli, "free_memory_gb", lambda: 11.0)
    assert (
        cli.cmd_status(roadmap, state, "s1", tmp_path / ".harness", config=config) == 0
    )
    out = capsys.readouterr().out
    assert "parallel 1/3 (free 11 GB, ~2.5 GB/agent)" in out


def test_parser_accepts_no_board_on_run_and_resume() -> None:
    args = cli.parser().parse_args(["run", "s1", "--no-board"])
    assert args.no_board is True
    args = cli.parser().parse_args(["resume", "s1", "--no-board"])
    assert args.no_board is True
