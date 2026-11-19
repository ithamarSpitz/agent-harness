from __future__ import annotations

from pathlib import Path

from harness.brief_lint import lint_brief
from harness.models import TaskSpec


BASE_BRIEF = """\
# Goal

Keep task briefs mechanically sound.

# Acceptance criteria

1. The requested behavior is covered.

# Rules

Stop if a test outside `allowed_paths` fails.
"""


def _task(
    allowed_paths: list[str] | None = None,
    *,
    allow_protected: bool = False,
) -> TaskSpec:
    return TaskSpec(
        id="S13-T1",
        file=Path("tasks/test.md"),
        sprint="S13",
        stage=1,
        allow_protected=allow_protected,
        allowed_paths=allowed_paths or [],
    )


def _codes(
    task: TaskSpec,
    text: str,
    root: Path,
    protected: list[str] | None = None,
) -> list[str]:
    return [finding.code for finding in lint_brief(task, text, root, protected or [])]


def test_protected_glob_respects_allow_protected(tmp_path: Path) -> None:
    task = _task(["tasks/**"])

    assert _codes(task, BASE_BRIEF, tmp_path, ["tasks/**"]) == ["protected"]
    assert _codes(
        _task(["tasks/**"], allow_protected=True),
        BASE_BRIEF,
        tmp_path,
        ["tasks/**"],
    ) == []


def test_missing_goal(tmp_path: Path) -> None:
    text = BASE_BRIEF.replace("# Goal\n", "")

    assert _codes(_task(), text, tmp_path) == ["missing-goal"]


def test_missing_criteria_when_section_has_no_list_item(tmp_path: Path) -> None:
    text = BASE_BRIEF.replace(
        "1. The requested behavior is covered.",
        "The requested behavior is covered.",
    )

    assert _codes(_task(), text, tmp_path) == ["missing-criteria"]


def test_stop_rule(tmp_path: Path) -> None:
    text = BASE_BRIEF.replace("Stop if a test outside `allowed_paths` fails.", "Run tests.")

    assert _codes(_task(), text, tmp_path) == ["stop-rule"]


def test_named_not_allowed_in_decisions(tmp_path: Path) -> None:
    text = BASE_BRIEF.replace(
        "# Acceptance criteria",
        "# Decisions (do not reopen)\n\nChange `harness/cli.py`.\n\n"
        "# Acceptance criteria",
    )

    assert _codes(_task(), text, tmp_path) == ["named-not-allowed"]


def test_path_named_only_in_background_is_not_checked(tmp_path: Path) -> None:
    text = BASE_BRIEF.replace(
        "# Acceptance criteria",
        "# Background\n\nThe current code is in `harness/cli.py`.\n\n"
        "# Acceptance criteria",
    )

    assert _codes(_task(), text, tmp_path) == []


def test_allowed_missing_for_unnamed_new_file(tmp_path: Path) -> None:
    task = _task(["harness/new_module.py", "tests/**"])

    assert _codes(task, BASE_BRIEF, tmp_path) == ["allowed-missing"]


def test_no_tests_for_python_implementation_path(tmp_path: Path) -> None:
    source = tmp_path / "harness" / "existing.py"
    source.parent.mkdir()
    source.write_text("", encoding="utf-8")

    assert _codes(_task(["harness/existing.py"]), BASE_BRIEF, tmp_path) == [
        "no-tests"
    ]


def test_too_big_for_more_than_six_acceptance_items(tmp_path: Path) -> None:
    items = "\n".join(f"{number}. Criterion {number}." for number in range(1, 8))
    text = BASE_BRIEF.replace("1. The requested behavior is covered.", items)

    assert _codes(_task(), text, tmp_path) == ["too-big"]


def test_clean_brief_has_no_findings(tmp_path: Path) -> None:
    source = tmp_path / "harness" / "brief_lint.py"
    test = tmp_path / "tests" / "harness" / "test_brief_lint.py"
    source.parent.mkdir(parents=True)
    test.parent.mkdir(parents=True)
    source.write_text("", encoding="utf-8")
    test.write_text("", encoding="utf-8")
    task = _task(
        [
            "harness/brief_lint.py",
            "tests/harness/test_brief_lint.py",
        ]
    )

    assert lint_brief(task, BASE_BRIEF, tmp_path, ["tasks/**"]) == []


def _test_not_allowed_repo(tmp_path: Path) -> None:
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "foo.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_a.py").write_text(
        "from harness.foo import x\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "test_b.py").write_text(
        "import os\n", encoding="utf-8"
    )


def test_test_not_allowed_warns_for_importing_test(tmp_path: Path) -> None:
    _test_not_allowed_repo(tmp_path)
    findings = lint_brief(_task(["harness/foo.py"]), BASE_BRIEF, tmp_path, [])
    hits = [f for f in findings if f.code == "test-not-allowed"]
    assert len(hits) == 1
    assert "tests/test_a.py" in hits[0].message
    assert hits[0].level == "warning"


def test_test_not_allowed_cleared_when_test_allowed(tmp_path: Path) -> None:
    _test_not_allowed_repo(tmp_path)
    task = _task(["harness/foo.py", "tests/test_a.py"])
    assert "test-not-allowed" not in _codes(task, BASE_BRIEF, tmp_path)


def test_test_not_allowed_import_forms_and_cap(tmp_path: Path) -> None:
    (tmp_path / "harness").mkdir()
    (tmp_path / "harness" / "foo.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    forms = [
        "import harness.foo\n",
        "from harness import bar, foo\n",
        "from .foo import x\n",
    ]
    for i in range(12):
        (tmp_path / "tests" / f"test_{i:02}.py").write_text(
            forms[i % 3], encoding="utf-8"
        )
    findings = lint_brief(_task(["harness/foo.py"]), BASE_BRIEF, tmp_path, [])
    msgs = [f.message for f in findings if f.code == "test-not-allowed"]
    assert len(msgs) == 11
    assert msgs[-1] == "... and 2 more"
