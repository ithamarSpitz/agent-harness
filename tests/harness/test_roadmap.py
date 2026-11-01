from pathlib import Path

import pytest

from harness.roadmap import Roadmap, RoadmapError


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_loads_task_frontmatter_and_roadmap_override(tmp_path: Path):
    write(tmp_path / "tasks/a.md", """---\nmodel_class: worker\nallowed_paths: [src/a.py]\n---\n# A\n""")
    write(tmp_path / "tasks/roadmap.yaml", """
project: x
sprints:
  s1:
    tasks:
      A:
        file: tasks/a.md
        stage: 2
        depends_on: []
""")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    task = roadmap.task("A")
    assert task.stage == 2
    assert task.allowed_paths == ["src/a.py"]
    assert task.model_class == "worker"


def test_rejects_dependency_cycle(tmp_path: Path):
    write(tmp_path / "tasks/a.md", "# A\n")
    write(tmp_path / "tasks/b.md", "# B\n")
    write(tmp_path / "tasks/roadmap.yaml", """
project: x
sprints:
  s1:
    tasks:
      A: {file: tasks/a.md, stage: 1, depends_on: [B]}
      B: {file: tasks/b.md, stage: 1, depends_on: [A]}
""")
    with pytest.raises(RoadmapError, match="cycle"):
        Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
