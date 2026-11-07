from __future__ import annotations

from pathlib import Path

from harness.config import HarnessConfig
from harness.roadmap import Roadmap
from harness.validation import ProjectValidator


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def base_config(tmp_path: Path) -> HarnessConfig:
    write(tmp_path / "harness.yaml", """
execution: {}
worktree: {}
cursor:
  models: {worker: null, reviewer: null}
verification: {}
paths: {}
context:
  aliases:
    arch: docs/arch.md
doctor: {}
""")
    write(tmp_path / "docs/arch.md", "# arch\n")
    return HarnessConfig.load(tmp_path / "harness.yaml")


def test_validator_accepts_well_formed_tasks_and_context(tmp_path: Path):
    config = base_config(tmp_path)
    write(tmp_path / "tasks/A.md", """---
id: A
model_class: worker
allowed_paths: [src/a.py]
context: [arch]
verification: [python -c \"print('ok')\"]
---
# A
""")
    write(tmp_path / "tasks/B.md", """---
id: B
model_class: worker
allowed_paths: [src/b.py]
verification: [python -c \"print('ok')\"]
---
# B
""")
    write(tmp_path / "tasks/roadmap.yaml", """
project: x
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
      B: {file: tasks/B.md, stage: 2, depends_on: [A]}
""")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    report = ProjectValidator(tmp_path, roadmap, config).validate()
    assert report.ok, report.render()


def test_validator_rejects_bad_stage_scope_verification_model_context_and_id(tmp_path: Path):
    config = base_config(tmp_path)
    write(tmp_path / "tasks/A.md", """---
id: WRONG
model_class: missing-model
allowed_paths: []
context: [does-not-exist]
verification: []
---
# A
""")
    write(tmp_path / "tasks/B.md", """---
id: B
model_class: worker
allowed_paths: [../escape]
verification: [echo ok]
---
# B
""")
    write(tmp_path / "tasks/roadmap.yaml", """
project: x
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: [B]}
      B: {file: tasks/B.md, stage: 1, depends_on: []}
""")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    report = ProjectValidator(tmp_path, roadmap, config).validate()
    rendered = report.render()
    assert not report.ok
    assert "frontmatter id" in rendered
    assert "allowed_paths" in rendered
    assert "verification" in rendered
    assert "model_class" in rendered
    assert "does-not-exist" in rendered
    assert "earlier stage" in rendered
    assert "repository-relative" in rendered


def test_validator_allows_future_docker_task_as_warning_until_prior_sprint_enables_it(tmp_path: Path):
    config = base_config(tmp_path)
    write(tmp_path / "tasks/A.md", """---
id: A
model_class: worker
environment: docker
allowed_paths: [src/a.py]
verification: [echo ok]
---
# A
""")
    write(tmp_path / "tasks/roadmap.yaml", """
project: x
sprints:
  future:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
""")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    report = ProjectValidator(tmp_path, roadmap, config).validate()
    assert report.ok, report.render()
    assert "environment=docker is declared" in report.render()


def test_sprint_validation_rejects_protected_path_unless_allowed(tmp_path: Path):
    config = base_config(tmp_path)
    config.paths.protected = ["harness/**"]
    brief_path = tmp_path / "tasks/A.md"
    roadmap_path = tmp_path / "tasks/roadmap.yaml"
    brief = """---
id: A
model_class: worker
allow_protected: false
allowed_paths: [harness/validation.py]
verification: [echo ok]
---
# Goal

Validate briefs.

# Acceptance criteria

- Protected paths are reported.

# Rules

- Stop if a test outside `allowed_paths` fails.
"""
    write(brief_path, brief)
    write(roadmap_path, """
project: x
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
""")

    roadmap = Roadmap(tmp_path, roadmap_path)
    report = ProjectValidator(tmp_path, roadmap, config).validate("s1")

    assert not report.ok
    assert any("A: brief protected:" in error for error in report.errors)

    write(brief_path, brief.replace("allow_protected: false", "allow_protected: true"))
    roadmap = Roadmap(tmp_path, roadmap_path)
    report = ProjectValidator(tmp_path, roadmap, config).validate("s1")

    assert report.ok, report.render()
    assert not any("brief protected:" in error for error in report.errors)


def test_sprint_validation_warns_for_missing_stop_rule_without_failing(tmp_path: Path):
    config = base_config(tmp_path)
    write(tmp_path / "src/a.py", "")
    write(tmp_path / "tasks/A.md", """---
id: A
model_class: worker
allowed_paths: [src/a.py]
verification: [echo ok]
---
# Goal

Validate briefs.

# Acceptance criteria

- Validation succeeds with warnings.
""")
    write(tmp_path / "tasks/roadmap.yaml", """
project: x
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
""")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")

    report = ProjectValidator(tmp_path, roadmap, config).validate("s1")

    assert report.ok, report.render()
    assert any("A: brief stop-rule:" in warning for warning in report.warnings)


def test_unscoped_validation_does_not_report_brief_findings(tmp_path: Path):
    config = base_config(tmp_path)
    config.paths.protected = ["harness/**"]
    write(tmp_path / "tasks/A.md", """---
id: A
model_class: worker
allowed_paths: [harness/validation.py]
verification: [echo ok]
---
# A
""")
    write(tmp_path / "tasks/roadmap.yaml", """
project: x
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
""")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")

    report = ProjectValidator(tmp_path, roadmap, config).validate()

    assert report.ok, report.render()
    assert not any("brief" in finding for finding in report.errors + report.warnings)
