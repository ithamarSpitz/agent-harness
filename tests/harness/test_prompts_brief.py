from __future__ import annotations

from pathlib import Path

from harness.config import UIReviewConfig
from harness.models import TaskSpec
from harness.prompts import implement_prompt, plan_change_prompt, review_prompt
from harness.roadmap import Roadmap

BRIEF_HEADING = "Task brief (current version)"
BRIEF_FOLLOW_INSTRUCTION = (
    "This embedded brief is the current version. If the copy in your worktree differs, "
    "follow the embedded one and do not edit the worktree copy."
)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _task(brief: Path, *, task_id: str = "T1") -> TaskSpec:
    return TaskSpec(
        id=task_id,
        file=brief,
        sprint="S1",
        stage=1,
        review=True,
        allowed_paths=["**"],
    )


def test_prompts_embed_current_brief_after_worktree_copy_diverges(tmp_path: Path) -> None:
    """Integration brief wins over an older worktree copy (fake files)."""
    integration = tmp_path / "project" / "tasks" / "T1.md"
    worktree_copy = tmp_path / "worktree" / "tasks" / "T1.md"
    write(worktree_copy, "---\nid: T1\n---\n# OLD brief still in worktree\n")
    write(integration, "---\nid: T1\n---\n# NEW brief after plan edit UNIQUE_MARKER_NEW\n")

    task = _task(integration.resolve())
    implement = implement_prompt(task)
    retry = implement_prompt(task, previous_failure="verification failed")
    review = review_prompt(task, "abc123", "ok")
    plan = plan_change_prompt(task, "blocked")

    for prompt in (implement, retry, review, plan):
        assert BRIEF_HEADING in prompt
        assert BRIEF_FOLLOW_INSTRUCTION in prompt
        assert "UNIQUE_MARKER_NEW" in prompt
        assert "OLD brief still in worktree" not in prompt

    assert "fresh retry context" in retry
    assert "verification failed" in retry


def test_unreadable_brief_falls_back_to_path_instruction(tmp_path: Path) -> None:
    missing = (tmp_path / "gone" / "missing-brief.md").resolve()
    task = _task(missing)

    implement = implement_prompt(task)
    review = review_prompt(task, "abc123", "ok")
    plan = plan_change_prompt(task, "blocked")

    path_text = missing.as_posix()
    for prompt in (implement, review, plan):
        assert path_text in prompt
        assert "fallback" in prompt.lower()
        assert BRIEF_HEADING not in prompt
        assert "UNIQUE_SHOULD_NOT_APPEAR" not in prompt

    assert f"read the task brief at {path_text}" in implement


def test_roadmap_relative_file_embeds_brief_from_other_cwd(
    tmp_path: Path, monkeypatch
) -> None:
    """Real loader + relative roadmap file:; prompts built from another cwd embed the brief."""
    project = tmp_path / "project"
    brief_body = "---\nmodel_class: worker\n---\n# Brief body MARKER_FROM_LOADER\n"
    write(project / "tasks" / "a.md", brief_body)
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

    implement = implement_prompt(task)
    retry = implement_prompt(task, previous_failure="retry reason")
    review = review_prompt(task, "deadbeef", "summary")

    for prompt in (implement, retry, review):
        assert BRIEF_HEADING in prompt
        assert BRIEF_FOLLOW_INSTRUCTION in prompt
        assert "MARKER_FROM_LOADER" in prompt
        assert brief_body in prompt or "# Brief body MARKER_FROM_LOADER" in prompt


def test_implement_and_retry_prompts_include_enabled_ui_review_instructions(
    tmp_path: Path,
) -> None:
    brief = tmp_path / "tasks" / "T1.md"
    write(brief, "# Task\n")
    task = _task(brief)
    ui_review = UIReviewConfig(
        enabled=True,
        start_commands=["python -m fake_app --port 4321"],
        url="http://127.0.0.1:4321/example",
    )

    implement = implement_prompt(task, ui_review=ui_review)
    retry = implement_prompt(task, previous_failure="failed", ui_review=ui_review)

    for prompt in (implement, retry):
        assert "use the Playwright tools" in prompt
        assert "Do not use other browser or computer-use tools" in prompt
        assert "python -m fake_app --port 4321" in prompt
        assert "http://127.0.0.1:4321/example" in prompt
        assert "Stop the app before finishing" in prompt


def test_disabled_ui_review_adds_no_browser_instruction(tmp_path: Path) -> None:
    brief = tmp_path / "tasks" / "T1.md"
    write(brief, "# Task\n")
    task = _task(brief)

    prompts = (
        implement_prompt(task),
        implement_prompt(task, previous_failure="failed"),
        implement_prompt(task, ui_review=UIReviewConfig(enabled=False)),
    )

    for prompt in prompts:
        assert "Playwright" not in prompt
        assert "browser" not in prompt
        assert "computer-use" not in prompt


def test_implement_ui_review_does_not_change_review_prompt(tmp_path: Path) -> None:
    brief = tmp_path / "tasks" / "T1.md"
    write(brief, "# Task\n")
    task = _task(brief)

    before = review_prompt(task, "abc123", "ok")
    implement_prompt(
        task,
        ui_review=UIReviewConfig(
            enabled=True,
            start_commands=["python -m fake_app"],
            url="http://127.0.0.1:8000",
        ),
    )

    assert review_prompt(task, "abc123", "ok") == before
