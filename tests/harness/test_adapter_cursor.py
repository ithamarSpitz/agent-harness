from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from harness.adapters.base import CallContext
from harness.adapters.cursor import CursorAdapter
from harness.runner import AgentResult


class FakeRunner:
    def __init__(self, result: AgentResult | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.result = result or AgentResult(True, "", "", provider="cursor")

    def implement(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls.append(("implement", args, kwargs))
        return self.result

    def review(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls.append(("review", args, kwargs))
        return self.result

    def plan_change(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls.append(("plan_change", args, kwargs))
        return self.result


def _config(models: dict[str, str | None] | None = None) -> Any:
    return SimpleNamespace(
        cursor=SimpleNamespace(enabled=True, command="agent", models=models or {})
    )


def _make(
    runner: FakeRunner, model_class: str = "worker", config: Any = None
) -> tuple[CursorAdapter, list[int]]:
    quota: list[int] = []
    adapter = CursorAdapter(
        runner,  # type: ignore[arg-type]
        config or _config(),
        model_class_for=lambda task: model_class,
        on_auto_quota=lambda: quota.append(1),
        budget=lambda: 3,
    )
    return adapter, quota


def _ctx(workspace: Path) -> CallContext:
    return CallContext(
        task="TASK",  # type: ignore[arg-type]
        workspace=workspace, timeout_minutes=5, attempt=2, provider_attempt=1,
        previous_failure="prev", env={"A": "b"}, model_class="worker",
        code_map_offered=True, on_process_start=None,
    )


def test_implement_passes_model_class(tmp_path: Path) -> None:
    runner = FakeRunner()
    adapter, quota = _make(runner, "hard_worker")
    adapter.implement(_ctx(tmp_path))
    name, args, kwargs = runner.calls[0]
    assert name == "implement"
    assert args == ("TASK", tmp_path, 5, 2, "prev")
    assert kwargs == {
        "env": {"A": "b"}, "model_class_override": "hard_worker",
        "code_map_offered": True, "on_process_start": None,
    }
    assert quota == []


def test_review_and_plan_change_arguments(tmp_path: Path) -> None:
    runner = FakeRunner()
    adapter, _ = _make(runner)
    task: Any = "T"
    adapter.review(
        task, tmp_path, "base", "sum", 7,
        env=None, code_map_offered=False, on_process_start=None,
    )
    adapter.plan_change(task, tmp_path, "fail", 9, model_class="worker", env=None)
    assert runner.calls[0] == (
        "review", ("T", tmp_path, "base", "sum", 7),
        {"env": None, "code_map_offered": False, "on_process_start": None},
    )
    assert runner.calls[1] == (
        "plan_change", ("T", tmp_path, "fail", 9),
        {"model_class": "worker", "env": None},
    )


def test_auto_quota_calls_hook_once(tmp_path: Path) -> None:
    runner = FakeRunner(AgentResult(False, "", "", provider="cursor", quota_exhausted=True))
    adapter, quota = _make(runner, "auto")
    adapter.implement(_ctx(tmp_path))
    assert quota == [1]


def test_named_model_quota_does_not_call_hook(tmp_path: Path) -> None:
    runner = FakeRunner(AgentResult(False, "", "", provider="cursor", quota_exhausted=True))
    adapter, quota = _make(runner, "worker")
    adapter.implement(_ctx(tmp_path))
    assert quota == []


def test_consumes_attempt() -> None:
    adapter, _ = _make(FakeRunner())

    def res(**kw: Any) -> AgentResult:
        return AgentResult(False, "", "", provider="cursor", **kw)

    assert not adapter.consumes_attempt(res(quota_exhausted=True))
    assert not adapter.consumes_attempt(res(waiting_cause="x"))
    assert not adapter.consumes_attempt(res(launch_failure=True))
    assert not adapter.consumes_attempt(res(resource_launch_failure=True))
    assert adapter.consumes_attempt(res())


def test_levels_budget_name_enabled() -> None:
    adapter, _ = _make(FakeRunner(), config=_config({"worker": "w", "hard_worker": "h"}))
    levels = adapter.levels()
    assert (levels.worker, levels.hard_worker, levels.escalation) == ("w", "h", None)
    assert adapter.implementation_budget() == 3
    assert adapter.name == "cursor" and adapter.enabled()
