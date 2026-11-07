from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import harness.cli as cli
from harness.codex_runner import CodexAgentRunner
from harness.models import TaskSpec
from harness.runner import AgentResult


class FakeCodex:
    def __init__(self, result: AgentResult):
        self.result = result
        self.calls: list[tuple[object, ...]] = []

    def availability(self, *, refresh: bool = False) -> tuple[bool, str]:
        assert refresh is True
        return True, "available"

    def brief_check(
        self,
        task: TaskSpec,
        workspace: Path,
        brief_text: str,
        timeout_minutes: int,
        *,
        model: str,
    ) -> AgentResult:
        self.calls.append((task, workspace, brief_text, timeout_minutes, model))
        return self.result


def _runtime(tmp_path: Path, result: AgentResult) -> tuple[SimpleNamespace, FakeCodex]:
    brief = tmp_path / "tasks" / "T1.md"
    brief.parent.mkdir()
    brief.write_text("# Brief\n", encoding="utf-8")
    task = TaskSpec(
        id="T1",
        file=brief,
        sprint="S1",
        stage=1,
        allowed_paths=["harness/cli.py"],
    )
    codex = FakeCodex(result)
    runtime = SimpleNamespace(
        root=tmp_path,
        config=SimpleNamespace(codex=SimpleNamespace(planner_model="planner")),
        roadmap=object(),
        runner=SimpleNamespace(codex=codex),
        scheduler=SimpleNamespace(
            task_queue=SimpleNamespace(resolve_task=lambda roadmap, task_id: task)
        ),
    )
    return runtime, codex


def test_matching_brief_check_exits_zero(monkeypatch, tmp_path: Path, capsys) -> None:
    result = AgentResult(
        True,
        "FILE: harness/cli.py\nPLAN: harness/cli.py: add the command\nEND\n",
    )
    runtime, codex = _runtime(tmp_path, result)
    monkeypatch.setattr(cli, "build_runtime", lambda root, roadmap, config: runtime)

    assert cli.main(["--root", str(tmp_path), "brief-check", "T1"]) == 0
    output = capsys.readouterr().out
    assert "harness/cli.py" in output
    assert "add the command" in output
    assert "OK brief reads as intended" in output
    assert codex.calls == [
        (runtime.scheduler.task_queue.resolve_task(None, "T1"), tmp_path, "# Brief\n", 10, "planner")
    ]


def test_brief_check_reports_file_outside_allowed_paths(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    result = AgentResult(
        True,
        "FILE: harness/cli.py\nFILE: README.md\n"
        "PLAN: harness/cli.py: add the command\nPLAN: README.md: document it\nEND\n",
    )
    runtime, _ = _runtime(tmp_path, result)
    monkeypatch.setattr(cli, "build_runtime", lambda root, roadmap, config: runtime)

    assert cli.main(["--root", str(tmp_path), "brief-check", "T1"]) == 1
    output = capsys.readouterr().out
    assert "File outside allowed_paths: README.md" in output


def test_brief_check_quota_exhaustion_exits_two(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    runtime, _ = _runtime(
        tmp_path,
        AgentResult(False, "", "Codex weekly usage quota exhausted", quota_exhausted=True),
    )
    monkeypatch.setattr(cli, "build_runtime", lambda root, roadmap, config: runtime)

    assert cli.main(["--root", str(tmp_path), "brief-check", "T1"]) == 2
    captured = capsys.readouterr()
    assert "Codex weekly usage quota exhausted" in captured.err
    assert captured.out == ""


def test_codex_brief_check_is_read_only(tmp_path: Path, monkeypatch) -> None:
    task = TaskSpec(id="T1", file=tmp_path / "brief.md", sprint="S1", stage=1)
    runner = CodexAgentRunner(SimpleNamespace(), tmp_path / "logs")
    captured: dict[str, object] = {}

    def fake_invoke(**kwargs: object) -> AgentResult:
        captured.update(kwargs)
        return AgentResult(True, "END\n")

    monkeypatch.setattr(runner, "_invoke", fake_invoke)

    runner.brief_check(task, tmp_path, "# Brief", 10, model="planner")

    assert captured["phase"] == "brief_check"
    assert captured["read_only"] is True
    assert captured["log_name"] == "T1-brief-check-codex.log"
