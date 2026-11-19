from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.adapters.base import CallContext
from harness.adapters.claude import ClaudeAdapter
from harness.agent_runner import AgentResult
from types import SimpleNamespace

from harness.config import ClaudeConfig


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def implement(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls.append(("implement", args, kwargs))
        return AgentResult(True, "i")

    def review(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls.append(("review", args, kwargs))
        return AgentResult(True, "r")

    def plan_change(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls.append(("plan_change", args, kwargs))
        return AgentResult(True, "p")


def make(enabled: bool = True, runner: Any = None) -> ClaudeAdapter:
    config: Any = SimpleNamespace(claude=ClaudeConfig(enabled=enabled))
    return ClaudeAdapter(config, runner)


def test_enabled() -> None:
    assert make(True, FakeRunner()).enabled()
    assert not make(True, None).enabled()
    assert not make(False, FakeRunner()).enabled()


def test_levels_default_and_budget() -> None:
    adapter = make()
    levels = adapter.levels()
    assert (levels.worker, levels.hard_worker, levels.escalation) == ("sonnet", "sonnet", "opus")
    assert adapter.implementation_budget() == 3


def test_implement_passes_scheduler_attempt() -> None:
    runner = FakeRunner()
    cb = lambda pid: None  # noqa: E731
    task: Any = object()
    ctx = CallContext(
        task, Path("w"), 7, 2, 5, "boom", {"A": "1"}, "worker", True, cb
    )
    make(True, runner).implement(ctx)
    name, args, kwargs = runner.calls[0]
    assert name == "implement"
    assert args == (task, Path("w"), 7, 2, "boom")
    assert kwargs == {"env": {"A": "1"}, "code_map_offered": True, "on_process_start": cb}


def test_review_and_plan_change_arguments() -> None:
    runner = FakeRunner()
    adapter = make(True, runner)
    task: Any = object()
    cb = lambda pid: None  # noqa: E731
    adapter.review(task, Path("w"), "base", "sum", 9, env=None, code_map_offered=False, on_process_start=cb)
    adapter.plan_change(task, Path("w"), "fail", 4, model_class="worker", env={"B": "2"})
    assert runner.calls[0] == (
        "review",
        (task, Path("w"), "base", "sum", 9),
        {"env": None, "code_map_offered": False, "on_process_start": cb},
    )
    assert runner.calls[1] == (
        "plan_change", (task, Path("w"), "fail", 4), {"env": {"B": "2"}}
    )
