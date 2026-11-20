"""Provider names must not appear in the router's dispatch methods."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PROVIDER_NAMES = {"codex", "cursor", "claude", "agy", "copilot", "kiro"}
CHECKED_METHODS = [
    "_choose",
    "_has_other_candidate",
    "max_implementation_attempts",
    "has_implementation_budget",
    "_implement_with_routing",
    "_review_with_routing",
    "_plan_change_with_routing",
    "brief_check",
]
SOURCE = Path(__file__).resolve().parents[2] / "harness" / "provider_runner.py"


def _is_provider_const(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value in PROVIDER_NAMES


def find_violations(source: str, method: str) -> list[str]:
    """Return 'method:line: reason' strings for provider-name uses in the method."""
    tree = ast.parse(source)
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for cls in ast.walk(tree):
        if isinstance(cls, ast.ClassDef) and cls.name == "ProviderAgentRunner":
            for item in cls.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name == method:
                    found.append(item)
    if not found:
        return [f"{method}: method not found on ProviderAgentRunner"]
    problems: list[str] = []
    for func in found:
        for node in ast.walk(func):
            if isinstance(node, ast.Compare):
                ops_ok = any(isinstance(op, (ast.Eq, ast.NotEq, ast.In, ast.NotIn)) for op in node.ops)
                operands = [node.left, *node.comparators]
                if ops_ok and any(_is_provider_const(o) or _container_has_provider(o) for o in operands):
                    problems.append(f"{method}:{node.lineno}: compares against a provider name")
            elif (
                isinstance(node, ast.Attribute)
                and node.attr in PROVIDER_NAMES
                and isinstance(node.value, ast.Name)
                and node.value.id == "self"
            ):
                problems.append(f"{method}:{node.lineno}: accesses self.{node.attr}")
    return problems


def _container_has_provider(node: ast.AST) -> bool:
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return any(_is_provider_const(e) for e in node.elts)
    return False


KNOWN_VIOLATIONS = {
    "_choose": "provider_runner.py lines 536, 551 still name a provider (self.codex)",
    "max_implementation_attempts": "provider_runner.py line 710 still compares a provider name",
    "has_implementation_budget": "provider_runner.py lines 746, 748 still compare a provider name",
}


def _params() -> list[object]:
    out: list[object] = []
    for m in CHECKED_METHODS:
        marks = (
            [pytest.mark.xfail(strict=True, reason=KNOWN_VIOLATIONS[m])] if m in KNOWN_VIOLATIONS else []
        )
        out.append(pytest.param(m, marks=marks))
    return out


@pytest.mark.parametrize("method", _params())
def test_dispatch_method_names_no_provider(method: str) -> None:
    problems = find_violations(SOURCE.read_text(encoding="utf-8"), method)
    assert not problems, "provider name in routing method: " + "; ".join(problems)


def test_checker_detects_string_comparison() -> None:
    src = (
        "class ProviderAgentRunner:\n"
        "    def _implement_with_routing(self, provider):\n"
        "        if provider == 'codex':\n"
        "            pass\n"
    )
    problems = find_violations(src, "_implement_with_routing")
    assert problems and "_implement_with_routing:3" in problems[0]


def test_checker_detects_in_and_attribute() -> None:
    src = (
        "class ProviderAgentRunner:\n"
        "    def brief_check(self, p):\n"
        "        a = p in ('cursor', 'x')\n"
        "        return self.copilot\n"
    )
    assert len(find_violations(src, "brief_check")) == 2


def test_checker_passes_clean_source() -> None:
    src = "class ProviderAgentRunner:\n    def _choose(self, p):\n        return p == 'other'\n"
    assert find_violations(src, "_choose") == []
