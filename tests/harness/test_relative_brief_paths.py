from __future__ import annotations

import json
from pathlib import Path

from harness.cli import build_runtime, cmd_inspect
from harness.models import TaskSpec
from harness.plan_change import _relative_brief_path, _task_signature
from harness.roadmap import Roadmap


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _minimal_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    write(project / "AGENTS.md", "# test\n")
    write(
        project / "harness.yaml",
        """
execution:
  max_parallel_agents: 1
  default_max_attempts: 1
cursor:
  command: agent
  models: {worker: null, reviewer: null}
verification:
  global_task_commands: []
  stage_commands: []
paths:
  protected: []
  review_required: []
""",
    )
    write(project / "tasks" / "a.md", "---\nmodel_class: worker\n---\n# A\n")
    write(
        project / "tasks" / "roadmap.yaml",
        """
project: x
sprints:
  s1:
    tasks:
      A:
        file: tasks/a.md
        stage: 1
        depends_on: []
""",
    )
    return project


def test_relative_brief_path_under_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    brief = (root / "tasks" / "a.md").resolve()
    brief.parent.mkdir(parents=True)
    brief.write_text("# A\n", encoding="utf-8")

    assert _relative_brief_path(brief, root) == "tasks/a.md"


def test_relative_brief_path_outside_root_unchanged(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    outside = (tmp_path / "elsewhere" / "brief.md").resolve()
    outside.parent.mkdir(parents=True)
    outside.write_text("# X\n", encoding="utf-8")

    assert _relative_brief_path(outside, root) == outside.as_posix()


def test_task_signature_matches_pre_s8_t36_relative_file(tmp_path: Path) -> None:
    root = tmp_path / "project"
    brief = root / "tasks" / "a.md"
    write(brief, "---\nmodel_class: worker\n---\n# A\n")

    absolute_task = TaskSpec(
        id="A",
        file=brief.resolve(),
        sprint="s1",
        stage=1,
        model_class="worker",
        allowed_paths=["**"],
    )
    relative_task = TaskSpec(
        id="A",
        file=Path("tasks/a.md"),
        sprint="s1",
        stage=1,
        model_class="worker",
        allowed_paths=["**"],
    )

    assert _task_signature(absolute_task, root.resolve()) == _task_signature(
        relative_task, root.resolve()
    )
    assert _task_signature(absolute_task, root.resolve())["file"] == "tasks/a.md"


def test_inspect_shows_relative_brief_path(tmp_path: Path, capsys) -> None:
    project = _minimal_project(tmp_path)
    runtime = build_runtime(project, project / "tasks/roadmap.yaml", project / "harness.yaml")

    assert runtime.roadmap.task("A").file.is_absolute()
    assert cmd_inspect(runtime, "A") == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["task"]["file"] == "tasks/a.md"
    assert not Path(payload["task"]["file"]).is_absolute()


def test_roadmap_loaded_signature_uses_relative_file(tmp_path: Path, monkeypatch) -> None:
    project = _minimal_project(tmp_path)
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.chdir(other)

    roadmap = Roadmap(project, project / "tasks" / "roadmap.yaml")
    task = roadmap.task("A")
    assert task.file.is_absolute()

    sig = _task_signature(task, project.resolve())
    assert sig["file"] == "tasks/a.md"
