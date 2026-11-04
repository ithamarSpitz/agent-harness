from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from harness.board import (
    BoardEvent,
    BoardSnapshot,
    BoardStage,
    BoardTask,
    render_board,
    snapshot_from_state,
)
from harness.models import SprintSpec, TaskSpec, TaskStatus
from harness.state import StateStore


def _sample_snapshot() -> BoardSnapshot:
    return BoardSnapshot(
        sprint_id="S8",
        sprint_title="Memory board",
        current_stage=2,
        stage_count=3,
        parallel_running=2,
        parallel_max=3,
        free_gb=10.5,
        per_agent_gb=4.0,
        stages=[
            BoardStage(
                number=1,
                merged=True,
                tasks=[
                    BoardTask(
                        id="S8-T1",
                        status=TaskStatus.DONE.value,
                        attempt=1,
                        max_attempts=2,
                    ),
                ],
            ),
            BoardStage(
                number=2,
                merged=False,
                tasks=[
                    BoardTask(
                        id="S8-T2",
                        status=TaskStatus.IMPLEMENTING.value,
                        provider="cursor",
                        model="worker",
                        attempt=1,
                        max_attempts=2,
                        phase_elapsed_seconds=725,
                        dependency_ids=["S8-T1"],
                    ),
                    BoardTask(
                        id="S8-T3",
                        status=TaskStatus.WAITING_QUOTA.value,
                        attempt=0,
                        max_attempts=2,
                    ),
                    BoardTask(
                        id="S8-T4",
                        status=TaskStatus.BLOCKED.value,
                        attempt=2,
                        max_attempts=2,
                        added=True,
                    ),
                ],
            ),
        ],
        events=[
            BoardEvent(datetime(2026, 11, 2, 14, 5, tzinfo=timezone.utc), "S8-T2 IMPLEMENTING"),
            BoardEvent(datetime(2026, 11, 2, 14, 4, tzinfo=timezone.utc), "stage 1 merged"),
            BoardEvent(datetime(2026, 11, 2, 14, 3, tzinfo=timezone.utc), "S8-T1 DONE"),
            BoardEvent(datetime(2026, 11, 2, 14, 2, tzinfo=timezone.utc), "S8-T2 started"),
            BoardEvent(datetime(2026, 11, 2, 14, 1, tzinfo=timezone.utc), "sprint started"),
            BoardEvent(datetime(2026, 11, 2, 14, 0, tzinfo=timezone.utc), "ignored sixth"),
        ],
    )


def test_render_board_unicode_exact_lines():
    snapshot = _sample_snapshot()
    assert render_board(snapshot, 120) == [
        "S8   Memory board   stage 2/3   parallel 2/3 (free 10.5 GB, ~4 GB/agent)",
        "Stage 1  ✓ merged",
        "  S8-T1 DONE try 1/2",
        "Stage 2",
        "  S8-T2 IMPLEMENTING cursor worker try 1/2 12m ← S8-T1",
        "  S8-T3 WAITING_QUOTA try 0/2",
        "  S8-T4 BLOCKED try 2/2 (added)",
        "",
        "14:05 S8-T2 IMPLEMENTING",
        "14:04 stage 1 merged",
        "14:03 S8-T1 DONE",
        "14:02 S8-T2 started",
        "14:01 sprint started",
    ]


def test_render_board_ascii():
    snapshot = _sample_snapshot()
    lines = render_board(snapshot, 120, unicode=False)
    assert lines[1] == "Stage 1  ok merged"
    assert "<- S8-T1" in lines[4]
    assert "✓" not in "".join(lines)
    assert "←" not in "".join(lines)
    assert all(line.isascii() for line in lines)


def test_render_board_truncates_to_width():
    snapshot = _sample_snapshot()
    for width in (40, 80, 120):
        lines = render_board(snapshot, width)
        assert lines
        for line in lines:
            assert len(line) <= width
            assert "\n" not in line


def test_render_board_shows_routing_reason_at_supported_widths():
    snapshot = BoardSnapshot(
        sprint_id="S9",
        sprint_title="Claude and routing",
        current_stage=5,
        stage_count=6,
        stages=[
            BoardStage(
                number=5,
                tasks=[
                    BoardTask(
                        id="S9-T12",
                        status=TaskStatus.IMPLEMENTING.value,
                        provider="claude",
                        model="sonnet",
                        routing_reason="claude: 58% of the five-hour quota free",
                    )
                ],
            )
        ],
    )

    for width in (60, 120):
        lines = render_board(snapshot, width)
        assert all(len(line) <= width for line in lines)
        assert lines == [
            "S9   Claude and routing   stage 5/6",
            "Stage 5",
            "  S9-T12 IMPLEMENTING claude sonnet try 0/2",
            "    claude: 58% of the five-hour quota free",
        ]


def test_render_board_does_not_show_stale_reason_for_inactive_task():
    snapshot = BoardSnapshot(
        sprint_id="S9",
        sprint_title="T",
        current_stage=1,
        stage_count=1,
        stages=[
            BoardStage(
                number=1,
                tasks=[
                    BoardTask(
                        id="T1",
                        status=TaskStatus.DONE.value,
                        routing_reason="old choice",
                    )
                ],
            )
        ],
    )
    assert render_board(snapshot, 120) == [
        "S9   T   stage 1/1",
        "Stage 1",
        "  T1 DONE try 0/2",
    ]


def test_render_board_keeps_long_routing_reason_on_one_indented_line():
    reason = "claude: most headroom\n" + "available capacity " * 10
    snapshot = BoardSnapshot(
        sprint_id="S9",
        sprint_title="Routing",
        current_stage=1,
        stage_count=1,
        stages=[BoardStage(number=1, tasks=[BoardTask(
            id="T1",
            status=TaskStatus.REVIEWING.value,
            provider="claude",
            model="sonnet",
            routing_reason=reason,
        )])],
    )
    for width in (60, 120):
        lines = render_board(snapshot, width)
        assert len(lines) == 4
        assert lines[-1] == ("    " + " ".join(reason.split()))[:width]
        assert all(len(line) <= width and "\n" not in line for line in lines)


def test_render_board_long_elapsed():
    snapshot = BoardSnapshot(
        sprint_id="S1",
        sprint_title="T",
        current_stage=1,
        stage_count=1,
        stages=[
            BoardStage(
                number=1,
                tasks=[
                    BoardTask(
                        id="T1",
                        status=TaskStatus.REVIEWING.value,
                        provider="codex",
                        model="gpt",
                        attempt=1,
                        max_attempts=1,
                        phase_elapsed_seconds=3905,
                    ),
                ],
            ),
        ],
    )
    lines = render_board(snapshot, 120)
    assert lines[2] == "  T1 REVIEWING codex gpt try 1/1 1h05m"


def test_render_board_events_newest_first():
    snapshot = BoardSnapshot(
        sprint_id="S1",
        sprint_title="T",
        current_stage=1,
        stage_count=1,
        events=[
            BoardEvent(datetime(2026, 11, 2, 10, 0, tzinfo=timezone.utc), "oldest"),
            BoardEvent(datetime(2026, 11, 2, 10, 3, tzinfo=timezone.utc), "newest"),
            BoardEvent(datetime(2026, 11, 2, 10, 1, tzinfo=timezone.utc), "middle"),
        ],
    )
    lines = render_board(snapshot, 80)
    assert lines[-3:] == [
        "10:03 newest",
        "10:01 middle",
        "10:00 oldest",
    ]


def test_render_board_omits_parallel_when_absent():
    snapshot = BoardSnapshot(
        sprint_id="S1",
        sprint_title="Title",
        current_stage=1,
        stage_count=1,
    )
    assert render_board(snapshot, 80) == ["S1   Title   stage 1/1"]


def test_snapshot_from_state_maps_fake_state(tmp_path: Path):
    sprint = SprintSpec(
        id="S8",
        name="Memory board",
        tasks={
            "S8-T1": TaskSpec(
                id="S8-T1",
                file=Path("tasks/a.md"),
                sprint="S8",
                stage=1,
                depends_on=[],
                max_attempts=3,
            ),
            "S8-T2": TaskSpec(
                id="S8-T2",
                file=Path("tasks/b.md"),
                sprint="S8",
                stage=2,
                depends_on=["S8-T1"],
            ),
        },
    )
    state = StateStore(tmp_path / "state.json")
    state.set_meta("S8.current_stage", 2)
    state.set_meta("S8.stage.1.merged", True)
    state.set_meta("S8.board.parallel_running", 1)
    state.set_meta("S8.board.parallel_max", 3)
    state.set_meta("S8.board.free_gb", 8)
    state.set_meta("resources.memory_per_agent_gb", 2.5)
    state.set_meta("S8.S8-T2.board.provider", "cursor")
    state.set_meta("S8.S8-T2.board.model", "worker")
    state.set_meta("S8.S8-T2.board.routing_reason", "cursor: most headroom")
    state.set_meta("S8.S8-T2.phase_started_at", "2026-11-02T12:00:00+00:00")

    done = state.get("S8-T1")
    done.status = TaskStatus.DONE
    done.attempt = 1

    running = state.get("S8-T2")
    running.status = TaskStatus.IMPLEMENTING
    running.attempt = 1
    running.added = True

    now = datetime(2026, 11, 2, 12, 12, 0, tzinfo=timezone.utc)
    events = [BoardEvent(now, "hello")]
    snapshot = snapshot_from_state(state, sprint, now, events=events)

    assert snapshot.sprint_id == "S8"
    assert snapshot.sprint_title == "Memory board"
    assert snapshot.current_stage == 2
    assert snapshot.stage_count == 2
    assert snapshot.parallel_running == 1
    assert snapshot.parallel_max == 3
    assert snapshot.free_gb == 8.0
    assert snapshot.per_agent_gb == 2.5
    assert snapshot.stages[0].merged is True
    assert snapshot.stages[0].tasks[0].status == TaskStatus.DONE.value
    assert snapshot.stages[1].tasks[0].provider == "cursor"
    assert snapshot.stages[1].tasks[0].model == "worker"
    assert snapshot.stages[1].tasks[0].routing_reason == "cursor: most headroom"
    assert snapshot.stages[1].tasks[0].phase_elapsed_seconds == 12 * 60
    assert snapshot.stages[1].tasks[0].added is True
    assert snapshot.stages[1].tasks[0].dependency_ids == ["S8-T1"]
    assert snapshot.events == events

    lines = render_board(snapshot, 120)
    assert lines[0].startswith("S8   Memory board   stage 2/2")
    assert "Stage 1  ✓ merged" in lines


def test_snapshot_from_state_derives_stage_and_elapsed(tmp_path: Path):
    sprint = SprintSpec(
        id="S8",
        name="Memory board",
        tasks={
            "S8-T1": TaskSpec(
                id="S8-T1",
                file=Path("tasks/a.md"),
                sprint="S8",
                stage=1,
                depends_on=[],
            ),
            "S8-T2": TaskSpec(
                id="S8-T2",
                file=Path("tasks/b.md"),
                sprint="S8",
                stage=2,
                depends_on=["S8-T1"],
                model_class="worker",
            ),
        },
    )
    state = StateStore(tmp_path / "state.json")
    done = state.get("S8-T1")
    done.status = TaskStatus.DONE
    running = state.get("S8-T2")
    running.status = TaskStatus.CHECKING
    running.attempt = 1
    running.started_at = "2026-11-02T12:00:00Z"
    running.waiting_provider = "cursor"

    now = datetime(2026, 11, 2, 12, 8, 0, tzinfo=timezone.utc)
    snapshot = snapshot_from_state(
        state,
        sprint,
        now,
        parallel_running=1,
        parallel_max=3,
        free_gb=10,
        per_agent_gb=4,
    )

    assert snapshot.current_stage == 2
    assert snapshot.stages[0].merged is True
    assert snapshot.stages[1].merged is False
    assert snapshot.stages[1].tasks[0].provider == "cursor"
    assert snapshot.stages[1].tasks[0].model == "worker"
    assert snapshot.stages[1].tasks[0].phase_elapsed_seconds == 8 * 60
    assert snapshot.parallel_running == 1
    header = render_board(snapshot, 120)[0]
    assert "parallel 1/3 (free 10 GB, ~4 GB/agent)" in header
    assert "CHECKING cursor worker" in render_board(snapshot, 120)[4]


def test_snapshot_from_state_reads_latest_recorded_routing_reason(tmp_path: Path):
    sprint = SprintSpec(
        id="S9",
        name="Routing",
        tasks={
            "T1": TaskSpec(id="T1", file=Path("tasks/t1.md"), sprint="S9", stage=1)
        },
    )
    state = StateStore(tmp_path / ".harness" / "state.json")
    state.get("T1").status = TaskStatus.REVIEWING
    usage_path = tmp_path / ".harness" / "usage.jsonl"
    usage_path.parent.mkdir(parents=True, exist_ok=True)
    usage_path.write_text(
        '\n'.join(
            [
                '{"event_type":"routing_choice","task_id":"T1",'
                '"routing_reason":"codex: earlier choice"}',
                '{"event_type":"agent_call","task_id":"T1"}',
                '{"event_type":"routing_choice","task_id":"T1",'
                '"routing_reason":"claude: 58% free"}',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    snapshot = snapshot_from_state(
        state,
        sprint,
        datetime(2026, 11, 3, tzinfo=timezone.utc),
    )

    assert snapshot.stages[0].tasks[0].routing_reason == "claude: 58% free"
    assert "    claude: 58% free" in render_board(snapshot, 120)
