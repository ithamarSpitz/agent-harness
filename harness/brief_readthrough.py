from __future__ import annotations

from dataclasses import dataclass

from .models import TaskSpec
from .verifier import _matches


@dataclass(frozen=True)
class Readthrough:
    files: list[str]
    plans: dict[str, str]
    unclear: list[str]


def readthrough_prompt(task: TaskSpec, brief_text: str) -> str:
    """Build the read-only prompt used to check how a model reads a task brief."""
    return f"""You are doing a read-only review of task {task.id}.
Do not create, change, or delete any files. Read the repository files you need to
understand the task, then describe the implementation you would make.

Answer ONLY with these line formats:
FILE: <repo path>
PLAN: <path>: <one line>
UNCLEAR: <question>
Use one FILE line for every repository file you would create or change, one PLAN
line for each planned file, and one UNCLEAR line for anything the brief leaves
open. Finish with a last line containing exactly:
END

Task brief:
--- BEGIN BRIEF ---
{brief_text}
--- END BRIEF ---
"""


def _normalize_path(path: str) -> str:
    normalized = path.strip()
    if len(normalized) >= 2 and normalized[0] == "`" and normalized[-1] == "`":
        normalized = normalized[1:-1].strip()
    return normalized.replace("\\", "/")


def parse_readthrough(output: str) -> Readthrough:
    files: list[str] = []
    plans: dict[str, str] = {}
    unclear: list[str] = []

    for raw_line in output.splitlines():
        line = raw_line.strip()
        if line.startswith("- "):
            line = line[2:].lstrip()

        if line.startswith("FILE:"):
            path = _normalize_path(line.removeprefix("FILE:"))
            if path:
                files.append(path)
        elif line.startswith("PLAN:"):
            plan = line.removeprefix("PLAN:").strip()
            if ":" not in plan:
                continue
            path, description = plan.split(":", 1)
            normalized_path = _normalize_path(path)
            description = description.strip()
            if normalized_path and description:
                plans[normalized_path] = description
        elif line.startswith("UNCLEAR:"):
            question = line.removeprefix("UNCLEAR:").strip()
            if question:
                unclear.append(question)

    return Readthrough(files=files, plans=plans, unclear=unclear)


def _is_glob(pattern: str) -> bool:
    return any(character in pattern for character in "*?[")


def compare_readthrough(rt: Readthrough, allowed_paths: list[str]) -> list[str]:
    normalized_allowed = [_normalize_path(path) for path in allowed_paths]
    messages = [
        f"File outside allowed_paths: {path}"
        for path in rt.files
        if not any(_matches(path, pattern) for pattern in normalized_allowed)
    ]

    for pattern in normalized_allowed:
        if not _is_glob(pattern) and not any(_matches(path, pattern) for path in rt.files):
            messages.append(f"Allowed file not touched: {pattern}")

    messages.extend(f"Unclear: {question}" for question in rt.unclear)
    return messages
