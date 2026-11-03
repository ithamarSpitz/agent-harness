from pathlib import Path

from harness.roadmap import Roadmap, task_spec_from_brief


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_roadmap_task_file_is_absolute_and_exists_from_other_cwd(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "project"
    brief = project / "tasks" / "a.md"
    write(brief, "---\nmodel_class: worker\n---\n# A\n")
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
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    roadmap = Roadmap(project, project / "tasks" / "roadmap.yaml")
    task = roadmap.task("A")

    assert task.file.is_absolute()
    assert task.file.exists()
    assert task.file == brief.resolve()


def test_task_spec_from_brief_stores_absolute_file(tmp_path: Path, monkeypatch) -> None:
    brief = tmp_path / "queued" / "B.md"
    write(brief, "---\nmodel_class: worker\n---\n# B\n")
    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    task = task_spec_from_brief("B", "s1", brief, stage=1)

    assert task.file.is_absolute()
    assert task.file.exists()
    assert task.file == brief.resolve()
