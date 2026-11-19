from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from harness.adapters import CallContext
from harness.adapters.agy import AgyAdapter
from harness.agent_runner import AgentResult
from harness.config import AgyConfig


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def implement(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls.append(("implement", args, kwargs))
        return AgentResult(True, "ok")

    def review(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls.append(("review", args, kwargs))
        return AgentResult(True, "ok")

    def plan_change(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls.append(("plan_change", args, kwargs))
        return AgentResult(True, "ok")


def make(runner: FakeRunner | None, enabled: bool = True) -> AgyAdapter:
    config: Any = SimpleNamespace(agy=AgyConfig(enabled=enabled))
    return AgyAdapter(config, runner, lambda task: {"gemini-weekly"})  # type: ignore[arg-type]


def test_enabled_requires_runner_and_flag() -> None:
    assert not make(None).enabled()
    assert not make(FakeRunner(), enabled=False).enabled()
    assert make(FakeRunner()).enabled()


def test_implement_passes_today_arguments() -> None:
    runner = FakeRunner()
    adapter = make(runner)
    task: Any = object()
    cb = lambda pid: None  # noqa: E731
    ctx = CallContext(task, Path("w"), 7, 2, 3, "prev", {"A": "1"}, "hard_worker", True, cb)
    adapter.implement(ctx)
    name, args, kwargs = runner.calls[0]
    assert name == "implement"
    assert args == (task, Path("w"), 7, 4, "prev")
    assert kwargs == {
        "env": {"A": "1"},
        "model_class_override": "hard_worker",
        "code_map_offered": True,
        "exhausted_pools": {"gemini-weekly"},
        "on_process_start": cb,
    }


def test_review_and_plan_change_arguments() -> None:
    runner = FakeRunner()
    adapter = make(runner)
    task: Any = object()
    adapter.review(task, Path("w"), "base", "sum", 5, env=None, code_map_offered=False, on_process_start=None)
    adapter.plan_change(task, Path("w"), "fail", 6, model_class="escalation", env={"B": "2"})
    assert runner.calls[0][1:] == (
        (task, Path("w"), "base", "sum", 5),
        {"env": None, "code_map_offered": False, "on_process_start": None},
    )
    assert runner.calls[1][1:] == (
        (task, Path("w"), "fail", 6),
        {"model_class": "escalation", "env": {"B": "2"}},
    )


def test_levels_budget_and_no_runner_call() -> None:
    adapter = make(FakeRunner())
    levels = adapter.levels()
    assert levels.worker == "gemini-3.8-flash-medium"
    assert levels.hard_worker == "gemini-3.8-flash-high"
    assert levels.escalation == "claude-opus-4-6-thinking"
    assert adapter.implementation_budget() == 3
    with pytest.raises(RuntimeError):
        make(None).review(Path("w"), Path("w"), "b", "s", 1, env=None, code_map_offered=False, on_process_start=None)  # type: ignore[arg-type]
