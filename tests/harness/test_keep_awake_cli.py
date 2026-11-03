from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

import harness.cli as cli
from harness.models import TaskRuntime, TaskSpec
from harness.scheduler import TaskOutcome


def _fake_keep_awake(monkeypatch, events: list[object]) -> None:
    class FakeKeepAwake:
        def __init__(self, enabled: bool):
            events.append(("init", enabled))

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append("exit")

    monkeypatch.setattr(cli, "KeepAwake", FakeKeepAwake)


@pytest.mark.parametrize("enabled", [True, False])
def test_run_task_holds_keep_awake_once(monkeypatch, enabled: bool) -> None:
    events: list[object] = []
    _fake_keep_awake(monkeypatch, events)
    task = TaskSpec(id="T1", file=Path("task.md"), sprint="s1", stage=1)
    task_runtime = TaskRuntime(base_ref="base")

    def run_task(task_arg, base_commit, *, model_class_override):
        assert events == [("init", enabled), "enter"]
        assert task_arg is task
        assert base_commit == "base"
        assert model_class_override is None
        return TaskOutcome(task_id="T1", ok=True)

    scheduler = SimpleNamespace(
        task_queue=SimpleNamespace(resolve_task=lambda roadmap, task_id: task),
        _run_task=run_task,
    )
    runtime = SimpleNamespace(
        roadmap=object(),
        scheduler=scheduler,
        validator=SimpleNamespace(validate=lambda sprint_id: SimpleNamespace(ok=True)),
        config=SimpleNamespace(
            cursor=SimpleNamespace(models={}),
            execution=SimpleNamespace(keep_awake_during_run=enabled),
        ),
        state=SimpleNamespace(get=lambda task_id: task_runtime),
        worktrees=SimpleNamespace(repo_root=Path(".")),
        failures=SimpleNamespace(resolve_task=lambda sprint_id, task_id: None),
    )

    assert cli.cmd_run_task(runtime, "T1") == 0
    assert events == [("init", enabled), "enter", "exit"]


@pytest.mark.parametrize("enabled", [True, False])
def test_watch_cli_holds_keep_awake_once(monkeypatch, tmp_path: Path, enabled: bool) -> None:
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


def test_status_cli_does_not_request_keep_awake(monkeypatch, tmp_path: Path) -> None:
    class ExplodingKeepAwake:
        def __init__(self, enabled: bool):
            raise AssertionError("status must not acquire keep-awake")

    runtime = SimpleNamespace(root=tmp_path, roadmap=object(), state=object())
    monkeypatch.setattr(cli, "KeepAwake", ExplodingKeepAwake)
    monkeypatch.setattr(cli, "build_runtime", lambda root, roadmap, config: runtime)
    monkeypatch.setattr(cli, "cmd_status", lambda roadmap, state, sprint, runtime_root: 23)

    assert cli.main(["--root", str(tmp_path), "status", "s1"]) == 23
