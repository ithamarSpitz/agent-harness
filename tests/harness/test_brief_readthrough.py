from pathlib import Path

from harness.brief_readthrough import (
    Readthrough,
    compare_readthrough,
    parse_readthrough,
    readthrough_prompt,
)
from harness.models import TaskSpec


def _task() -> TaskSpec:
    return TaskSpec(id="S13-T4", file=Path("tasks/S13-T4.md"), sprint="S13", stage=1)


def test_readthrough_prompt_contains_brief_and_required_instructions() -> None:
    brief = "# Goal\nMake the brief readable."

    prompt = readthrough_prompt(_task(), brief)

    assert brief in prompt
    assert "read-only" in prompt
    assert "FILE: <repo path>" in prompt
    assert "PLAN: <path>: <one line>" in prompt
    assert "UNCLEAR: <question>" in prompt
    assert "END" in prompt


def test_parse_readthrough_tolerates_noise_and_normalizes_paths() -> None:
    output = """
This preliminary sentence should be ignored.

- FILE: `harness\\brief_readthrough.py`
PLAN: `harness\\brief_readthrough.py`: Add the pure parsing helpers.
UNCLEAR: Should duplicate FILE lines be retained?
END
Another ignored line.
"""

    assert parse_readthrough(output) == Readthrough(
        files=["harness/brief_readthrough.py"],
        plans={"harness/brief_readthrough.py": "Add the pure parsing helpers."},
        unclear=["Should duplicate FILE lines be retained?"],
    )


def test_compare_readthrough_reports_each_problem() -> None:
    readthrough = Readthrough(
        files=["harness/brief_readthrough.py", "README.md"],
        plans={},
        unclear=["Which wording?", "Should paths be normalized?"],
    )

    assert compare_readthrough(
        readthrough,
        ["harness/brief_readthrough.py", "tests/harness/test_brief_readthrough.py"],
    ) == [
        "File outside allowed_paths: README.md",
        "Allowed file not touched: tests/harness/test_brief_readthrough.py",
        "Unclear: Which wording?",
        "Unclear: Should paths be normalized?",
    ]


def test_compare_readthrough_accepts_matching_files_and_globs() -> None:
    readthrough = Readthrough(
        files=["harness/brief_readthrough.py", "tests/harness/test_brief_readthrough.py"],
        plans={
            "harness/brief_readthrough.py": "Implement helpers.",
            "tests/harness/test_brief_readthrough.py": "Test helpers.",
        },
        unclear=[],
    )

    assert compare_readthrough(
        readthrough,
        ["harness/brief_readthrough.py", "tests/harness/**"],
    ) == []
