"""Outside-diff users of symbols changed in a task worktree.

Used to pre-populate the review prompt so the reviewer does not have to
discover callers via the optional code map.
"""

from __future__ import annotations

import ast
import json
import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

LOGGER = logging.getLogger(__name__)

MAX_IMPACT_LINES = 40
IMPACT_RELATIONS = frozenset({"calls", "imports_from", "uses", "references"})
OUTSIDE_IMPACT_HEADER = "Code outside the diff that uses what changed:"

_HUNK_RE = re.compile(r"^@@\s+-\d+(?:,\d+)?\s+\+(\d+)(?:,(\d+))?\s+@@")
_SOURCE_LOC_RE = re.compile(r"L(\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class ChangedSymbol:
    """A Python function, class, or method overlapping the task diff."""

    name: str
    qualname: str
    file: str
    kind: str  # "function" | "class" | "method"


def _run_git(workspace: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def _posix(path: str) -> str:
    return path.replace("\\", "/")


def _parse_diff_new_ranges(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Map path -> inclusive 1-based line ranges on the new (worktree) side."""
    ranges: dict[str, list[tuple[int, int]]] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current = None
            continue
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith("b/"):
                path = path[2:]
            if path == "/dev/null":
                current = None
            else:
                current = _posix(path)
                ranges.setdefault(current, [])
            continue
        if current is None:
            continue
        match = _HUNK_RE.match(line)
        if not match:
            continue
        start = int(match.group(1))
        length = int(match.group(2) or "1")
        if length <= 0:
            continue
        ranges[current].append((start, start + length - 1))
    return ranges


def _ranges_overlap(start: int, end: int, ranges: list[tuple[int, int]]) -> bool:
    for r0, r1 in ranges:
        if start <= r1 and end >= r0:
            return True
    return False


def _node_span(node: ast.AST) -> tuple[int, int] | None:
    lineno = getattr(node, "lineno", None)
    if not isinstance(lineno, int):
        return None
    end = getattr(node, "end_lineno", None)
    if not isinstance(end, int):
        end = lineno
    return lineno, end


def _symbols_from_ast(
    tree: ast.AST,
    file: str,
    ranges: list[tuple[int, int]] | None,
) -> list[ChangedSymbol]:
    """Collect defs overlapping *ranges*; when ranges is None, take every def."""
    found: list[ChangedSymbol] = []

    def consider(node: ast.AST, name: str, qualname: str, kind: str) -> None:
        span = _node_span(node)
        if span is None:
            return
        start, end = span
        if ranges is not None and not _ranges_overlap(start, end, ranges):
            return
        found.append(ChangedSymbol(name=name, qualname=qualname, file=file, kind=kind))

    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            consider(node, node.name, node.name, "function")
        elif isinstance(node, ast.ClassDef):
            consider(node, node.name, node.name, "class")
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    consider(
                        child,
                        child.name,
                        f"{node.name}.{child.name}",
                        "method",
                    )
    return found


def _untracked_files(workspace: Path) -> list[str]:
    result = _run_git(workspace, ["ls-files", "--others", "--exclude-standard"])
    if result.returncode != 0:
        return []
    return [_posix(line.strip()) for line in result.stdout.splitlines() if line.strip()]


def find_changed_symbols(workspace: Path, base_ref: str) -> list[ChangedSymbol]:
    """Return changed Python functions/classes/methods vs *base_ref*."""
    diff = _run_git(workspace, ["diff", base_ref, "--"])
    ranges_by_file = _parse_diff_new_ranges(diff.stdout if diff.returncode == 0 else "")
    symbols: list[ChangedSymbol] = []
    seen: set[tuple[str, str, str]] = set()

    def add_all(items: list[ChangedSymbol]) -> None:
        for item in items:
            key = (item.file, item.qualname, item.kind)
            if key in seen:
                continue
            seen.add(key)
            symbols.append(item)

    for file, ranges in ranges_by_file.items():
        if not file.endswith(".py") or not ranges:
            continue
        path = workspace / file
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=file)
        except (OSError, UnicodeError, SyntaxError):
            continue
        add_all(_symbols_from_ast(tree, file, ranges))

    for file in _untracked_files(workspace):
        if not file.endswith(".py"):
            continue
        if file in ranges_by_file:
            continue
        path = workspace / file
        if not path.is_file():
            continue
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=file)
        except (OSError, UnicodeError, SyntaxError):
            continue
        add_all(_symbols_from_ast(tree, file, None))

    return symbols


def _parse_source_line(location: object) -> int | None:
    if isinstance(location, int):
        return location
    if not isinstance(location, str):
        return None
    match = _SOURCE_LOC_RE.search(location)
    if match:
        return int(match.group(1))
    if location.isdigit():
        return int(location)
    return None


def _node_matches_symbol(node: dict[str, object], symbol: ChangedSymbol) -> bool:
    source = _posix(str(node.get("source_file") or ""))
    if source != symbol.file:
        return False
    label = str(node.get("label") or "")
    name = symbol.name
    if symbol.kind == "class":
        return label == name
    if symbol.kind == "method":
        return label in {f".{name}()", f"{name}()", name}
    return label in {f"{name}()", name}


def _users_from_graph(
    workspace: Path,
    symbols: list[ChangedSymbol],
    changed_files: set[str],
) -> list[str] | None:
    """Return formatted user lines from graph.json, or None to trigger fallback."""
    graph_path = workspace / "graphify-out" / "graph.json"
    if not graph_path.is_file():
        return None
    try:
        data = json.loads(graph_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        LOGGER.warning("failed to read code map for review impact: %s", exc)
        return None
    if not isinstance(data, dict):
        LOGGER.warning("failed to read code map for review impact: expected a JSON object")
        return None
    nodes = data.get("nodes")
    links = data.get("links")
    if not isinstance(nodes, list) or not isinstance(links, list):
        LOGGER.warning("failed to read code map for review impact: missing nodes/links")
        return None

    target_ids: dict[str, ChangedSymbol] = {}
    for node in nodes:
        if not isinstance(node, dict):
            continue
        node_id = node.get("id")
        if not isinstance(node_id, str):
            continue
        for symbol in symbols:
            if _node_matches_symbol(node, symbol):
                target_ids[node_id] = symbol
                break

    lines: list[str] = []
    seen: set[str] = set()
    for link in links:
        if not isinstance(link, dict):
            continue
        relation = link.get("relation")
        if relation not in IMPACT_RELATIONS:
            continue
        target = link.get("target")
        if not isinstance(target, str) or target not in target_ids:
            continue
        source_file = _posix(str(link.get("source_file") or ""))
        if not source_file or source_file in changed_files:
            continue
        line_no = _parse_source_line(link.get("source_location"))
        if line_no is None:
            continue
        symbol = target_ids[target]
        entry = f"{source_file}:{line_no} -> {symbol.qualname}"
        if entry in seen:
            continue
        seen.add(entry)
        lines.append(entry)
    lines.sort()
    return lines


def _users_from_grep(
    workspace: Path,
    symbols: list[ChangedSymbol],
    changed_files: set[str],
) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    names = sorted({symbol.name for symbol in symbols})
    qual_by_name: dict[str, str] = {}
    for symbol in symbols:
        qual_by_name.setdefault(symbol.name, symbol.qualname)

    excludes = [f":(exclude){path}" for path in sorted(changed_files)]
    for name in names:
        if not name.isidentifier():
            continue
        # Pathspec excludes; Git accepts :(exclude) on all platforms.
        cmd = ["grep", "-n", "-I", "-w", name, "--", ".", *excludes]
        result = _run_git(workspace, cmd)
        if result.returncode not in (0, 1):
            # Retry without excludes if pathspec failed; filter in Python.
            result = _run_git(workspace, ["grep", "-n", "-I", "-w", name, "--", "."])
            if result.returncode not in (0, 1):
                continue
        for raw in result.stdout.splitlines():
            # path:line:content
            parts = raw.split(":", 2)
            if len(parts) < 2:
                continue
            file = _posix(parts[0])
            if file in changed_files:
                continue
            try:
                line_no = int(parts[1])
            except ValueError:
                continue
            entry = f"{file}:{line_no} -> {qual_by_name.get(name, name)}"
            if entry in seen:
                continue
            seen.add(entry)
            lines.append(entry)
    lines.sort()
    return lines


def _cap_lines(lines: list[str]) -> list[str]:
    if len(lines) <= MAX_IMPACT_LINES:
        return lines
    kept = lines[:MAX_IMPACT_LINES]
    more = len(lines) - MAX_IMPACT_LINES
    kept.append(f"and {more} more")
    return kept


def format_outside_impact_lines(lines: list[str]) -> str:
    """Format the impact body (under the section header)."""
    capped = _cap_lines(lines)
    if not capped:
        return "none found"
    return "\n".join(capped)


def collect_outside_impact(workspace: Path, base_ref: str) -> str:
    """List outside-diff users of changed symbols; never raises."""
    try:
        symbols = find_changed_symbols(workspace, base_ref)
        if not symbols:
            return format_outside_impact_lines([])
        changed_files = {symbol.file for symbol in symbols}
        # Also exclude every file touched in the diff, even without symbols.
        diff = _run_git(workspace, ["diff", "--name-only", base_ref, "--"])
        if diff.returncode == 0:
            changed_files.update(
                _posix(line.strip()) for line in diff.stdout.splitlines() if line.strip()
            )
        changed_files.update(_untracked_files(workspace))

        graph_lines = _users_from_graph(workspace, symbols, changed_files)
        if graph_lines is not None:
            return format_outside_impact_lines(graph_lines)
        return format_outside_impact_lines(
            _users_from_grep(workspace, symbols, changed_files)
        )
    except Exception as exc:  # noqa: BLE001 - must never fail the review
        LOGGER.warning("review impact collection failed: %s", exc)
        return format_outside_impact_lines([])


def outside_impact_section(body: str) -> str:
    """Full prompt section including the header."""
    return f"{OUTSIDE_IMPACT_HEADER}\n{body}"
