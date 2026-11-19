from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.adapters.base import CallContext
from harness.adapters.codex import CodexAdapter
from harness.agent_runner import AgentResult
from harness.config import HarnessConfig
from harness.models import TaskSpec


def _config(tmp_path: Path) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text("codex:\n  enabled: true\n", encoding="utf-8")
    return HarnessConfig.load(path)


class FakeRunner:
    def __init__(self, available: bool = True) -> None:
        self.available = available
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def availability(self, *, refresh: bool = False) -> tuple[bool, str]:
        return self.available, "detail"

    def implement(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls.append(("implement", args, kwargs))
        return AgentResult(True, "ok", provider="codex")

    def review(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls.append(("review", args, kwargs))
        return AgentResult(True, "ok", provider="codex")

    def plan_change(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls.append(("plan_change", args, kwargs))
        return AgentResult(True, "ok", provider="codex")

    def brief_check(self, *args: Any, **kwargs: Any) -> AgentResult:
        self.calls.append(("brief_check", args, kwargs))
        return AgentResult(True, "ok", provider="codex")


def _task(tmp_path: Path) -> TaskSpec:
    task_file = tmp_path / "task.md"
    task_file.write_text("# task\n", encoding="utf-8")
    return TaskSpec(id="T1", file=task_file, sprint="S1", stage=1, review=True, allowed_paths=["**"])


def _marker(pid: int) -> None:
    return None


def _ctx(tmp_path: Path, provider_attempt: int) -> CallContext:
    return CallContext(
        task=_task(tmp_path), workspace=tmp_path, timeout_minutes=7, attempt=3,
        provider_attempt=provider_attempt, previous_failure="boom", env={"A": "b"},
        model_class="worker", code_map_offered=True, on_process_start=_marker,
    )


def test_implement_uses_profile_per_provider_attempt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    for index in range(len(config.codex.implementation_sequence)):
        runner = FakeRunner()
        ctx = _ctx(tmp_path, index)
        assert CodexAdapter(runner, config).implement(ctx).ok
        _, args, kwargs = runner.calls[0]
        model, effort = config.codex.implementation_profile(index)
        assert args == (ctx.task, tmp_path, 7, 3, "boom")
        assert kwargs == {
            "model": model, "reasoning_effort": effort, "env": {"A": "b"},
            "code_map_offered": True, "on_process_start": _marker,
        }


def test_unavailable_never_calls_runner(tmp_path: Path) -> None:
    runner = FakeRunner(available=False)
    adapter = CodexAdapter(runner, _config(tmp_path))
    task = _task(tmp_path)
    results = [
        adapter.implement(_ctx(tmp_path, 0)),
        adapter.review(task, tmp_path, "base", "sum", 5, env=None, code_map_offered=False,
                       on_process_start=None),
        adapter.plan_change(task, tmp_path, "fail", 5, model_class="worker", env=None),
        adapter.brief_check(task, tmp_path, "brief", 5),
    ]
    for result in results:
        assert not result.ok
        assert result.auth_failure
        assert result.provider == "codex"
        assert result.fallback_reason == "auth_or_cli_unavailable"
    assert runner.calls == []


def test_review_and_plan_change_arguments(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = FakeRunner()
    adapter = CodexAdapter(runner, config)
    task = _task(tmp_path)
    adapter.review(task, tmp_path, "base", "sum", 5, env=None, code_map_offered=True,
                   on_process_start=None)
    adapter.plan_change(task, tmp_path, "fail", 6, model_class="worker", env={"X": "y"})
    assert runner.calls[0][2] == {
        "model": config.codex.review_model, "env": None, "code_map_offered": True,
        "on_process_start": None,
    }
    assert runner.calls[1][1] == (task, tmp_path, "fail", 6)
    assert runner.calls[1][2] == {"model": config.codex.planner_model, "env": {"X": "y"}}


def test_levels_defaults_budget_enabled(tmp_path: Path) -> None:
    config = _config(tmp_path)
    adapter = CodexAdapter(FakeRunner(), config)
    levels = adapter.levels()
    assert (levels.worker, levels.hard_worker, levels.escalation) == (
        "gpt-5.6-sol", "gpt-5.6-sol", "gpt-6-astra",
    )
    assert adapter.implementation_budget() == 4
    assert adapter.enabled() is config.codex.enabled
    assert adapter.name == "codex"


def test_find_signed_in(tmp_path: Path) -> None:
    found = CodexAdapter(FakeRunner(), _config(tmp_path)).find(refresh=True)
    assert found.installed and found.signed_in is True


def test_brief_check_arguments(tmp_path: Path) -> None:
    config = _config(tmp_path)
    runner = FakeRunner()
    task = _task(tmp_path)
    adapter = CodexAdapter(runner, config)
    assert adapter.brief_check(task, tmp_path, "brief", 4).ok
    assert adapter.brief_check(task, tmp_path, "brief", 4, env={"X": "y"}).ok
    assert runner.calls[0][1] == (task, tmp_path, "brief", 4)
    assert runner.calls[0][2] == {"model": config.codex.planner_model, "env": None}
    assert runner.calls[1][2] == {"model": config.codex.planner_model, "env": {"X": "y"}}
