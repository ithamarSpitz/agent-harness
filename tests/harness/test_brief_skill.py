from pathlib import Path

import yaml


SKILL_PATH = (
    Path(__file__).resolve().parents[2] / "skills" / "write-harness-briefs" / "SKILL.md"
)


def _skill_parts() -> tuple[dict[str, object], str]:
    text = SKILL_PATH.read_text(encoding="utf-8")
    opening, frontmatter, body = text.split("---", maxsplit=2)
    assert opening == ""
    loaded = yaml.safe_load(frontmatter)
    assert isinstance(loaded, dict)
    return loaded, body


def test_brief_writer_skill_has_required_frontmatter() -> None:
    frontmatter, _body = _skill_parts()

    assert frontmatter["name"] == "write-harness-briefs"
    assert isinstance(frontmatter["description"], str)
    assert frontmatter["description"].strip()


def test_brief_writer_skill_names_required_checks_and_commands() -> None:
    _frontmatter, body = _skill_parts()

    for required in (
        "harness validate",
        "harness brief-check",
        "allowed_paths",
        "allow_protected",
        "outside allowed_paths",
    ):
        assert required in body


def test_brief_writer_skill_uses_validator_recognized_brief_headings() -> None:
    _frontmatter, body = _skill_parts()

    for heading in (
        "## Goal",
        "## Background",
        "## Decisions (do not reopen)",
        "## Acceptance criteria",
        "## Rules",
    ):
        assert heading in body.splitlines()
