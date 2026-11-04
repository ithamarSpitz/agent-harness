"""Build per-call shell permissions for Antigravity CLI (``agy --settings``).

Headless agy cannot ask for permission: any shell command without a matching
rule is silently denied. The installed agy (1.4.12) matches ``command(...)``
rules against exact commands only; ``*`` wildcards and ``regex:`` rules are not
supported, so they are never emitted. Wildcard defaults whose commands the
harness prompts name are expanded to those exact commands; rules whose
arguments cannot be known when the call is built (``graphify ...``, any other
wildcard) are dropped with one warning.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence

LOGGER = logging.getLogger(__name__)

_COMMAND_RULE = re.compile(r"^command\((?P<command>.*)\)$", re.DOTALL)

# Wildcard defaults expanded to the exact commands the harness prompts use.
_PROMPT_COMMAND_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "command(git status*)": ("command(git status)",),
    "command(git diff*)": ("command(git diff)", "command(git diff --stat)"),
    "command(git log*)": ("command(git log --oneline -20)",),
}


def _split_shell_operators(command: str) -> list[str]:
    """Split ``command`` at unquoted ``&&``, ``|`` and ``;``.

    Operators inside single or double quotes, or escaped with a backslash
    (outside single quotes), belong to the command and are kept as is.
    """
    parts: list[str] = []
    current: list[str] = []
    quote: str | None = None
    index = 0
    length = len(command)
    while index < length:
        char = command[index]
        if quote == "'":
            current.append(char)
            if char == "'":
                quote = None
        elif char == "\\" and index + 1 < length:
            current.append(command[index : index + 2])
            index += 2
            continue
        elif quote == '"':
            current.append(char)
            if char == '"':
                quote = None
        elif char in ("'", '"'):
            current.append(char)
            quote = char
        elif command.startswith("&&", index):
            parts.append("".join(current))
            current = []
            index += 2
            continue
        elif char in "|;":
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append("".join(current))
    return [part.strip() for part in parts if part.strip()]


def _verification_rules(commands: Sequence[str]) -> list[str]:
    return [
        f"command({part})"
        for command in commands
        for part in _split_shell_operators(command)
    ]


def _base_sha_rules(base_sha: str | None) -> list[str]:
    sha = (base_sha or "").strip()
    if not sha:
        return []
    return [
        f"command(git diff {sha})",
        f"command(git diff {sha} --stat)",
        f"command(git diff --stat {sha})",
    ]


def _is_unsupported(rule: str) -> bool:
    """True for rules agy cannot match exactly or that need the code map."""
    if "*" in rule or rule.strip().lower().startswith("regex:"):
        return True
    match = _COMMAND_RULE.match(rule.strip())
    if match is None:
        return False
    command = match.group("command").strip()
    return command == "graphify" or command.startswith("graphify ")


def build_settings(
    task_verification: Sequence[str],
    base_allow: Sequence[str],
    extra_allow: Sequence[str] = (),
    base_sha: str | None = None,
) -> str:
    """Return stable, deduplicated per-call ``agy --settings`` JSON.

    Order: verification parts, ``base_allow`` (wildcards expanded), the exact
    ``git diff`` rules for ``base_sha``, then ``extra_allow``. Review calls pass
    no verification so they get only ``base_allow`` (plus ``base_sha`` rules).
    """
    candidates = [
        *_verification_rules(task_verification),
        *(
            expanded
            for rule in base_allow
            for expanded in _PROMPT_COMMAND_EXPANSIONS.get(rule.strip(), (rule,))
        ),
        *_base_sha_rules(base_sha),
        *extra_allow,
    ]
    allow: list[str] = []
    dropped: list[str] = []
    for rule in dict.fromkeys(candidates):
        (dropped if _is_unsupported(rule) else allow).append(rule)
    if dropped:
        LOGGER.warning(
            "agy permissions: dropped rules agy cannot match exactly: %s",
            ", ".join(dropped),
        )
    return json.dumps({"permissions": {"allow": allow}}, separators=(",", ":"))
