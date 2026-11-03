from __future__ import annotations

from pathlib import Path

from harness.models import TaskSpec
from harness.prompts import implement_prompt, review_prompt


def _task(brief: Path | None = None, *, task_id: str = "T15") -> TaskSpec:
    return TaskSpec(
        id=task_id,
        file=brief or Path(f"tasks/{task_id}.md"),
        sprint="S9",
        stage=1,
        review=True,
        allowed_paths=["**"],
    )


IMPACT_HEADER = "Before the verdict, complete this required impact check:"
IMPACT_LIST_STEP = (
    "List every changed function, class, config key, CLI command or output format."
)
OUTSIDE_DIFF_CHECK = (
    "effects on code outside the diff (callers, importers, config and CLI users)"
)
IMPACT_SECTION = (
    "After the VERDICT line, write a section starting with the line `IMPACT:` "
    "listing each changed item and the users you checked (or `none outside the diff`)."
)
OPTIONAL_CODE_MAP = (
    "A code map of this repository is available in graphify-out/. You decide whether it helps."
)


def test_review_prompt_impact_without_code_map() -> None:
    prompt = review_prompt(_task(), "abc123", "ok", code_map_offered=False)

    assert IMPACT_HEADER in prompt
    assert IMPACT_LIST_STEP in prompt
    assert "use `git grep`" in prompt
    assert OUTSIDE_DIFF_CHECK in prompt
    assert IMPACT_SECTION in prompt
    assert prompt.index(IMPACT_HEADER) < prompt.index("VERDICT: PASS")
    assert prompt.index("VERDICT: PASS") < prompt.index(IMPACT_SECTION)
    assert "graphify query" not in prompt
    assert "required for this step" not in prompt


def test_review_prompt_impact_with_code_map_requires_graphify() -> None:
    prompt = review_prompt(
        _task(),
        "abc123",
        "ok",
        code_map_offered=True,
        code_map_command="graphify",
    )

    assert IMPACT_HEADER in prompt
    assert IMPACT_LIST_STEP in prompt
    assert OUTSIDE_DIFF_CHECK in prompt
    assert IMPACT_SECTION in prompt
    assert "use the code map (required for this step)" in prompt
    assert "graphify-out/AGENTS-graphify.md" in prompt
    assert "`graphify query`" in prompt
    assert "--budget 500" in prompt
    assert "use `git grep`" not in prompt
    assert prompt.index(IMPACT_HEADER) < prompt.index("VERDICT: PASS")
    assert prompt.index("VERDICT: PASS") < prompt.index(IMPACT_SECTION)


def test_review_prompt_impact_uses_code_map_command_token() -> None:
    prompt = review_prompt(
        _task(),
        "deadbeef",
        "summary",
        code_map_offered=True,
        code_map_command="my-graphify --flag",
    )
    assert "`my-graphify query`" in prompt
    assert "use the code map (required for this step)" in prompt


def test_implement_and_retry_prompts_unchanged_optional_code_map() -> None:
    task = _task()
    implement = implement_prompt(task, code_map_offered=False)
    retry = implement_prompt(task, previous_failure="failed", code_map_offered=False)
    implement_map = implement_prompt(task, code_map_offered=True, code_map_command="graphify")
    retry_map = implement_prompt(
        task,
        previous_failure="failed",
        code_map_offered=True,
        code_map_command="graphify",
    )

    for prompt in (implement, retry, implement_map, retry_map):
        assert IMPACT_HEADER not in prompt
        assert OUTSIDE_DIFF_CHECK not in prompt
        assert IMPACT_SECTION not in prompt
        assert "required for this step" not in prompt

    assert OPTIONAL_CODE_MAP in implement_map
    assert OPTIONAL_CODE_MAP in retry_map
    assert OPTIONAL_CODE_MAP not in implement
    assert OPTIONAL_CODE_MAP not in retry
