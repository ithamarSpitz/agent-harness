from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.cli import cmd_add_task, cmd_rerun_blocker
from harness.console_commands import (
    COMMANDS,
    COMMAND_BY_NAME,
    ConsoleContext,
    QuitSignal,
    capture_cli,
    run_command,
)
from harness.models import SprintSpec, TaskRuntime, TaskSpec, TaskStatus
from harness.state import StateStore


def _sprint() -> SprintSpec:
    return SprintSpec(
        id="S8",
        name="Memory board",
        tasks={
            "S8-T1": TaskSpec(
                id="S8-T1",
                file=Path("tasks/a.md"),
                sprint="S8",
                stage=1,
                allowed_paths=["harness/a.py"],
                depends_on=[],
            ),
            "S8-T2": TaskSpec(
                id="S8-T2",
                file=Path("tasks/b.md"),
                sprint="S8",
                stage=2,
                model_class="worker",
                allowed_paths=["harness/b.py", "tests/harness/test_b.py"],
                depends_on=["S8-T1"],
            ),
        },
    )


def _fake_runtime(tmp_path: Path, *, sprint: SprintSpec | None = None) -> SimpleNamespace:
    sprint = sprint or _sprint()
    state = StateStore(tmp_path / ".harness" / "state.json")
    roadmap = SimpleNamespace(
        sprint=lambda sprint_id: sprint,
        task=lambda task_id: sprint.tasks[task_id],
    )
    failures = SimpleNamespace(get=lambda _sprint_id: None)
    return SimpleNamespace(
        root=tmp_path,
        state=state,
        roadmap=roadmap,
        failures=failures,
        config=SimpleNamespace(execution=SimpleNamespace(default_max_attempts=2)),
    )


def _context(
    runtime: SimpleNamespace,
    *,
    confirm=None,
    call_cli=None,
    now: datetime | None = None,
    git_diff_stat=None,
) -> ConsoleContext:
    return ConsoleContext(
        runtime=runtime,
        sprint_id="S8",
        confirm=confirm or (lambda _prompt: False),
        call_cli=call_cli or capture_cli,
        now=now or datetime(2026, 11, 2, 12, 12, tzinfo=timezone.utc),
        git_diff_stat=git_diff_stat,
    )


def test_help_lists_every_registered_command(tmp_path: Path):
    runtime = _fake_runtime(tmp_path)
    lines = run_command("help", _context(runtime))
    names = [command.name for command in COMMANDS]
    assert names == [
        "help",
        "status",
        "task",
        "why",
        "log",
        "diff",
        "usage",
        "add",
        "retry",
        "stop",
        "quit",
    ]
    assert len(lines) == len(COMMANDS)
    for command, line in zip(COMMANDS, lines, strict=True):
        assert line.startswith(command.name)
        assert command.help in line
    assert set(COMMAND_BY_NAME) == set(names)
    assert COMMAND_BY_NAME["add"].changes_state is True
    assert COMMAND_BY_NAME["retry"].changes_state is True
    assert COMMAND_BY_NAME["stop"].changes_state is True
    assert COMMAND_BY_NAME["quit"].changes_state is False


def test_status_is_empty_and_unknown_command_points_to_help(tmp_path: Path):
    ctx = _context(_fake_runtime(tmp_path))
    assert run_command("status", ctx) == []
    assert run_command("nope", ctx) == ["Unknown command: nope. Type 'help' for a list."]


def test_task_why_log_diff_usage_for_fake_runtime(tmp_path: Path):
    runtime = _fake_runtime(tmp_path)
    state = runtime.state
    state.set_meta("S8.S8-T2.board.provider", "cursor")
    state.set_meta("S8.S8-T2.board.model", "worker")
    state.set_meta("S8.S8-T2.phase_started_at", "2026-11-02T12:00:00+00:00")

    done = state.get("S8-T1")
    done.status = TaskStatus.DONE
    done.attempt = 1

    worktree = tmp_path / "wt"
    worktree.mkdir()
    running = state.get("S8-T2")
    running.status = TaskStatus.IMPLEMENTING
    running.attempt = 1
    running.added = True
    running.worktree = str(worktree)
    running.base_ref = "BASE"
    running.review_feedback = "Reviewer: fix the flaky test\nAlso trim imports"
    running.last_error = "should not win over review feedback"
    state.save()

    logs = tmp_path / ".harness" / "logs"
    logs.mkdir(parents=True)
    older = logs / "S8-T2-attempt-1.log"
    newer = logs / "S8-T2-review.log"
    older.write_text("old message\n", encoding="utf-8")
    newer.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "draft"}]},
                    }
                ),
                json.dumps({"type": "result", "result": "VERDICT: FAIL\nfix the allowed_paths"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    # Ensure review log is newest even on coarse filesystem mtimes.
    older_stat = older.stat()
    newer.touch()
    if newer.stat().st_mtime <= older_stat.st_mtime:
        import os

        os.utime(newer, (older_stat.st_mtime + 5, older_stat.st_mtime + 5))

    def fake_diff(worktree: Path, base_ref: str) -> list[str]:
        assert worktree == Path(running.worktree)
        assert base_ref == "BASE"
        return [" harness/b.py | 2 +-", " 1 file changed, 1 insertion(+), 1 deletion(-)"]

    limits_calls: list[object] = []

    def fake_call_cli(func, *args, **kwargs):
        limits_calls.append(func)
        return ["Codex: ok", "", "Cursor: 10% used"]

    ctx = _context(
        runtime,
        call_cli=fake_call_cli,
        git_diff_stat=fake_diff,
    )

    assert run_command("task S8-T2", ctx) == [
        "task: S8-T2",
        "status: IMPLEMENTING",
        "provider: cursor",
        "model: worker",
        "attempt: 1",
        "elapsed: 12m",
        "allowed_paths: harness/b.py, tests/harness/test_b.py",
        "depends_on: S8-T1",
        "added: yes",
    ]
    assert run_command("why S8-T2", ctx) == [
        "Reviewer: fix the flaky test",
        "Also trim imports",
    ]
    assert run_command("log S8-T2", ctx) == [
        "VERDICT: FAIL",
        "fix the allowed_paths",
    ]
    assert run_command("diff S8-T2", ctx) == [
        " harness/b.py | 2 +-",
        " 1 file changed, 1 insertion(+), 1 deletion(-)",
    ]
    assert run_command("usage", ctx) == ["Codex: ok", "", "Cursor: 10% used"]
    assert limits_calls and limits_calls[0].__name__ == "run_limits"

    assert run_command("task missing", ctx) == ["Unknown task: missing"]
    assert run_command("why missing", ctx) == ["Unknown task: missing"]
    assert run_command("log missing", ctx) == ["Unknown task: missing"]
    assert run_command("diff missing", ctx) == ["Unknown task: missing"]


def test_why_falls_back_to_last_error_and_log_without_message(tmp_path: Path):
    runtime = _fake_runtime(tmp_path)
    rt = runtime.state.get("S8-T1")
    rt.status = TaskStatus.BLOCKED
    rt.last_error = "Verification failed: ruff"
    runtime.state.save()

    ctx = _context(runtime)
    assert run_command("why S8-T1", ctx) == ["Verification failed: ruff"]
    assert run_command("log S8-T1", ctx) == ["No agent log for S8-T1."]
    assert run_command("diff S8-T1", ctx) == ["No worktree/base for S8-T1."]


def test_log_extracts_codex_agent_message(tmp_path: Path):
    runtime = _fake_runtime(tmp_path)
    runtime.state.get("S8-T1").status = TaskStatus.REVIEWING
    runtime.state.save()
    log_dir = tmp_path / ".harness" / "logs"
    log_dir.mkdir(parents=True)
    (log_dir / "S8-T1-attempt-1-codex.log").write_text(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "final codex note"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert run_command("log S8-T1", _context(runtime)) == ["final codex note"]


def test_read_only_commands_leave_state_json_byte_identical(tmp_path: Path):
    runtime = _fake_runtime(tmp_path)
    state_path = Path(runtime.state.path)
    task = runtime.state.get("S8-T2")
    task.status = TaskStatus.BLOCKED
    task.attempt = 2
    task.last_error = "boom"
    task.worktree = str(tmp_path / "missing-wt")
    task.base_ref = "abc"
    runtime.state.set_meta("S8.note", "keep-me")
    runtime.state.save()
    before = state_path.read_bytes()

    logs = tmp_path / ".harness" / "logs"
    logs.mkdir(parents=True)
    (logs / "S8-T2-attempt-1.log").write_text("hello from agent\n", encoding="utf-8")

    ctx = _context(
        runtime,
        call_cli=lambda *_a, **_k: ["limits"],
        git_diff_stat=lambda *_a, **_k: ["diff"],
    )
    for line in (
        "help",
        "status",
        "task S8-T2",
        "why S8-T2",
        "log S8-T2",
        "diff S8-T2",
        "usage",
        "task missing",
        "why missing",
    ):
        run_command(line, ctx)

    assert state_path.read_bytes() == before
    # Ensure we did not replace TaskRuntime with a different object shape via get().
    assert isinstance(runtime.state.get("S8-T2"), TaskRuntime)


def test_change_commands_need_confirm_and_call_cli(tmp_path: Path):
    runtime = _fake_runtime(tmp_path)
    blocked = runtime.state.get("S8-T1")
    blocked.status = TaskStatus.BLOCKED
    runtime.state.save()
    state_before = Path(runtime.state.path).read_bytes()
    stop_path = tmp_path / ".harness" / "stop-request"
    brief = tmp_path / "tasks" / "new.md"
    brief.parent.mkdir(parents=True)
    brief.write_text("# new\n", encoding="utf-8")

    cli_calls: list[tuple[object, tuple[object, ...], dict[str, object]]] = []

    def fake_call_cli(func, *args, **kwargs):
        cli_calls.append((func, args, kwargs))
        return [f"called:{func.__name__}"]

    denied = _context(runtime, confirm=lambda _p: False, call_cli=fake_call_cli)
    assert run_command(f"add {brief.as_posix()}", denied) == ["Cancelled."]
    assert run_command("add tasks/new.md --stage next", denied) == ["Cancelled."]
    assert run_command("retry S8-T1", denied) == ["Cancelled."]
    assert run_command("stop", denied) == ["Cancelled."]
    assert cli_calls == []
    assert not stop_path.exists()
    assert Path(runtime.state.path).read_bytes() == state_before

    prompts: list[str] = []

    def accept(prompt: str) -> bool:
        prompts.append(prompt)
        return True

    accepted = _context(runtime, confirm=accept, call_cli=fake_call_cli)
    assert run_command(f"add {brief.as_posix()} --stage current", accepted) == [
        "called:cmd_add_task"
    ]
    assert cli_calls[-1][0] is cmd_add_task
    assert cli_calls[-1][1] == (runtime, "S8", brief.resolve())
    assert cli_calls[-1][2] == {"stage_choice": "current"}

    assert run_command("retry S8-T1", accepted) == ["called:cmd_rerun_blocker"]
    assert cli_calls[-1][0] is cmd_rerun_blocker
    assert cli_calls[-1][1] == (runtime, "S8", None, "S8-T1")
    assert cli_calls[-1][2] == {}

    assert run_command("stop", accepted) == ["Stop requested for S8."]
    assert stop_path.is_file()
    payload = json.loads(stop_path.read_text(encoding="utf-8"))
    assert payload == {
        "sprint": "S8",
        "time": "2026-11-02T12:12:00+00:00",
    }
    assert any("stage=current" in prompt for prompt in prompts)
    assert any("S8-T1" in prompt for prompt in prompts)


def test_retry_rejects_non_blocked_without_changes(tmp_path: Path):
    runtime = _fake_runtime(tmp_path)
    running = runtime.state.get("S8-T2")
    running.status = TaskStatus.IMPLEMENTING
    runtime.state.save()
    state_before = Path(runtime.state.path).read_bytes()
    cli_calls: list[object] = []

    def fake_call_cli(func, *args, **kwargs):
        cli_calls.append(func)
        return ["should not run"]

    ctx = _context(runtime, confirm=lambda _p: True, call_cli=fake_call_cli)
    assert run_command("retry S8-T2", ctx) == [
        "S8-T2 is IMPLEMENTING, not BLOCKED; cannot retry."
    ]
    assert run_command("retry missing", ctx) == ["Unknown task: missing"]
    assert cli_calls == []
    assert Path(runtime.state.path).read_bytes() == state_before


def test_quit_raises_without_confirm_or_side_effects(tmp_path: Path):
    runtime = _fake_runtime(tmp_path)
    runtime.state.get("S8-T1").status = TaskStatus.BLOCKED
    runtime.state.save()
    state_before = Path(runtime.state.path).read_bytes()
    confirms: list[str] = []
    cli_calls: list[object] = []

    ctx = _context(
        runtime,
        confirm=lambda prompt: confirms.append(prompt) or True,
        call_cli=lambda func, *a, **k: cli_calls.append(func) or ["x"],
    )
    with pytest.raises(QuitSignal):
        run_command("quit", ctx)
    assert confirms == []
    assert cli_calls == []
    assert not (tmp_path / ".harness" / "stop-request").exists()
    assert Path(runtime.state.path).read_bytes() == state_before
