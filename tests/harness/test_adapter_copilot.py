from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from harness.adapters.base import CallContext
from harness.adapters.copilot import CopilotAdapter
from harness.agent_runner import AgentResult
from harness.config import CopilotConfig


class FakeRunner:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[str, tuple, dict]] = []

    def availability(self, *, refresh: bool = False):
        return (self.available, "" if self.available else "nope")

    def _rec(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return AgentResult(True, "ok")

    def implement(self, *a, **k):
        return self._rec("implement", *a, **k)

    def review(self, *a, **k):
        return self._rec("review", *a, **k)

    def plan_change(self, *a, **k):
        return self._rec("plan_change", *a, **k)

    def brief_check(self, *a, **k):
        return self._rec("brief_check", *a, **k)


def make(runner, enabled=True):
    return CopilotAdapter(runner, SimpleNamespace(copilot=CopilotConfig(enabled=enabled)))


def test_enabled():
    assert make(FakeRunner()).enabled()
    assert not make(FakeRunner(), enabled=False).enabled()
    assert not make(None).enabled()


def test_delegation():
    runner = FakeRunner()
    adapter = make(runner)
    task, ws, cb = object(), Path("."), lambda pid: None
    ctx = CallContext(task, ws, 5, 1, 2, "prev", {"A": "1"}, "worker", True, cb)
    adapter.implement(ctx)
    adapter.review(task, ws, "base", "sum", 5, env=None, code_map_offered=True, on_process_start=cb)
    adapter.plan_change(task, ws, "fail", 5, model_class="reviewer", env=None)
    adapter.brief_check(task, ws, "brief", 5, env={"A": "1"})
    assert runner.calls == [
        ("implement", (task, ws, 5, 3, "prev"), {
            "env": {"A": "1"}, "model_class_override": "worker",
            "code_map_offered": True, "on_process_start": cb}),
        ("review", (task, ws, "base", "sum", 5), {
            "env": None, "code_map_offered": True, "on_process_start": cb}),
        ("plan_change", (task, ws, "fail", 5), {"model_class": "reviewer", "env": None}),
        ("brief_check", (task, ws, "brief", 5), {"env": {"A": "1"}}),
    ]


def test_brief_check_unavailable():
    runner = FakeRunner(available=False)
    result = make(runner).brief_check(object(), Path("."), "b", 5)
    assert not result.ok and result.auth_failure and result.provider == "copilot"
    assert result.output == "" and result.error == "Copilot CLI/auth unavailable"
    assert runner.calls == []


def test_levels_and_budget():
    adapter = make(FakeRunner())
    levels = adapter.levels()
    assert (levels.worker, levels.hard_worker, levels.escalation) == (
        "auto:balance", "auto:intelligence", "auto:intelligence")
    assert adapter.implementation_budget() == 3
