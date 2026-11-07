from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from harness.models import TaskSpec
from harness.verifier import _matches

VALID_REPO_EXTENSIONS = (
    ".py",
    ".md",
    ".yaml",
    ".yml",
    ".toml",
    ".json",
    ".js",
    ".ts",
)
BACKTICK_RE = re.compile(r"`([^`\r\n]+)`")
HEADING_RE = re.compile(r"^ {0,3}(#{1,2})\s+(.+)$")
LIST_ITEM_RE = re.compile(r"^\s*(?:\d+[.)]|[-*+])\s+\S")


@dataclass(frozen=True)
class BriefFinding:
    level: str  # "error" or "warning"
    code: str
    message: str


@dataclass
class _Section:
    level: int
    title: str
    lines: list[str]

    @property
    def content(self) -> str:
        return "\n".join(self.lines)


def _split_frontmatter(text: str) -> tuple[str, str]:
    """Return (frontmatter, body). If no frontmatter, frontmatter is empty."""
    lines = text.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for idx in range(1, len(lines)):
            if lines[idx].strip() == "---":
                return "".join(lines[1:idx]), "".join(lines[idx + 1:])
    return "", text


def _find_sections(body: str, prefix: str) -> list[_Section]:
    """Find all sections whose heading title matches prefix case-insensitively.

    A section starts at a level 1 or 2 heading and runs until the next heading of
    the same or higher level (level 1 > level 2).
    """
    lines = body.splitlines()
    headings: list[tuple[int, int, str]] = []
    for idx, line in enumerate(lines):
        m = HEADING_RE.match(line)
        if m:
            level = len(m.group(1))
            title = m.group(2).strip().rstrip("#").strip()
            headings.append((idx, level, title))

    matching_sections: list[_Section] = []
    prefix_lower = prefix.lower()

    for pos, (idx, level, title) in enumerate(headings):
        if not title.lower().startswith(prefix_lower):
            continue

        end_idx = len(lines)
        for next_idx, next_level, _ in headings[pos + 1:]:
            if next_level <= level:
                end_idx = next_idx
                break

        matching_sections.append(
            _Section(
                level=level,
                title=title,
                lines=lines[idx + 1:end_idx],
            )
        )

    return matching_sections


def _is_glob(pattern: str) -> bool:
    return any(ch in pattern for ch in ("*", "?", "["))


def _glob_prefix(pattern: str) -> str:
    indices = [pattern.find(ch) for ch in ("*", "?", "[") if pattern.find(ch) != -1]
    if not indices:
        return pattern
    return pattern[: min(indices)]


def _matches_protected(entry: str, protected_patterns: list[str]) -> str | None:
    test_path = _glob_prefix(entry) if _is_glob(entry) else entry
    for pat in protected_patterns:
        if _matches(test_path, pat) or (
            test_path.endswith("/") and _matches(test_path.rstrip("/"), pat)
        ):
            return pat
    return None


def _extract_repo_paths(text: str) -> list[str]:
    paths: list[str] = []
    for match in BACKTICK_RE.finditer(text):
        candidate = match.group(1).strip()
        if (
            "/" in candidate
            and candidate.endswith(VALID_REPO_EXTENSIONS)
        ):
            paths.append(candidate)
    return paths


def _exists_under_root(root: Path, entry: str) -> bool:
    relative = Path(entry)
    if relative.is_absolute() or relative.drive or ".." in relative.parts:
        return False
    return (root / relative).exists()


def lint_brief(
    task: TaskSpec,
    text: str,
    root: Path,
    protected: list[str],
) -> list[BriefFinding]:
    findings: list[BriefFinding] = []
    _, body = _split_frontmatter(text)

    # 1. protected (error)
    if not task.allow_protected:
        for entry in task.allowed_paths:
            matched_pat = _matches_protected(entry, protected)
            if matched_pat:
                findings.append(
                    BriefFinding(
                        level="error",
                        code="protected",
                        message=f"allowed_paths entry '{entry}' matches protected pattern '{matched_pat}'",
                    )
                )

    # 2. missing-goal (warning)
    goal_sections = _find_sections(body, "goal")
    if not goal_sections:
        findings.append(
            BriefFinding(
                level="warning",
                code="missing-goal",
                message="brief has no Goal heading",
            )
        )

    # 3. missing-criteria (warning)
    criteria_sections = _find_sections(body, "acceptance criteria")
    criteria_items: list[str] = []
    if not criteria_sections:
        findings.append(
            BriefFinding(
                level="warning",
                code="missing-criteria",
                message="brief has no Acceptance criteria heading",
            )
        )
    else:
        criteria_items = [
            line
            for sec in criteria_sections
            for line in sec.lines
            if LIST_ITEM_RE.match(line)
        ]
        if not criteria_items:
            findings.append(
                BriefFinding(
                    level="warning",
                    code="missing-criteria",
                    message="Acceptance criteria heading has no list items",
                )
            )

    # 4. stop-rule (warning)
    text_lower = text.lower()
    if (
        "outside allowed_paths" not in text_lower
        and "outside `allowed_paths`" not in text_lower
    ):
        findings.append(
            BriefFinding(
                level="warning",
                code="stop-rule",
                message="brief does not contain rule to stop if test outside allowed_paths fails",
            )
        )

    # 5. named-not-allowed (warning)
    decisions_sections = _find_sections(body, "decisions")
    checked_text = "\n".join(
        sec.content for sec in decisions_sections + criteria_sections
    )
    repo_paths = _extract_repo_paths(checked_text)
    seen_named: set[str] = set()
    for p in repo_paths:
        if p in seen_named:
            continue
        seen_named.add(p)
        if not any(_matches(p, pat) for pat in task.allowed_paths):
            findings.append(
                BriefFinding(
                    level="warning",
                    code="named-not-allowed",
                    message=f"path '{p}' named in Decisions or Acceptance criteria is not in allowed_paths",
                )
            )

    # 6. allowed-missing (warning)
    for entry in task.allowed_paths:
        if not _is_glob(entry):
            cleaned_entry = entry.lstrip("/\\")
            if not _exists_under_root(root, cleaned_entry) and entry not in body:
                findings.append(
                    BriefFinding(
                        level="warning",
                        code="allowed-missing",
                        message=f"allowed_paths entry '{entry}' does not exist under root and is not named in brief body",
                    )
                )

    # 7. no-tests (warning)
    py_outside = [
        entry
        for entry in task.allowed_paths
        if entry.endswith(".py")
        and not (entry == "tests" or entry.startswith("tests/"))
    ]
    has_tests = any(
        entry == "tests" or entry.startswith("tests/")
        for entry in task.allowed_paths
    )
    if py_outside and not has_tests:
        findings.append(
            BriefFinding(
                level="warning",
                code="no-tests",
                message=f"allowed_paths has Python file '{py_outside[0]}' outside tests/ but no entry under tests/",
            )
        )

    # 8. too-big (warning)
    if len(task.allowed_paths) > 6:
        findings.append(
            BriefFinding(
                level="warning",
                code="too-big",
                message=f"brief has {len(task.allowed_paths)} allowed_paths entries (more than 6); consider splitting",
            )
        )
    if len(criteria_items) > 6:
        findings.append(
            BriefFinding(
                level="warning",
                code="too-big",
                message=f"Acceptance criteria has {len(criteria_items)} items (more than 6); consider splitting",
            )
        )

    # Sort errors first, then by code, then message
    findings.sort(key=lambda f: (0 if f.level == "error" else 1, f.code, f.message))
    return findings
