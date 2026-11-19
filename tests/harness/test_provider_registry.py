from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

import harness.provider_runner as provider_runner
from harness.adapters.base import BaseAdapter
from harness.config import HarnessConfig
from harness.models import TaskSpec
from harness.provider_runner import ProviderAgentRunner
from harness.state import StateStore


class _Runner:
    log_dir = None

    def availability(self, refresh: bool = False):
        return True, ""


def _router(tmp_path: Path, *, copilot_enabled: bool = True) -> ProviderAgentRunner:
    path = tmp_path / "harness.yaml"
    path.write_text(
        yaml.safe_dump({
            "cursor": {"enabled": True},
            "codex": {"enabled": True},
            "copilot": {"enabled": copilot_enabled},
        }),
        encoding="utf-8",
    )
    config = HarnessConfig.load(path)
    return ProviderAgentRunner(
        config, StateStore(tmp_path / "state.json"), _Runner(), _Runner(),  # type: ignore[arg-type]
        copilot=_Runner(),  # type: ignore[arg-type]
        headroom_reader=lambda: {},
    )


def _task() -> TaskSpec:
    return SimpleNamespace(id="T1", sprint="S1", review_model_class=None)  # type: ignore[return-value]


def _spy(monkeypatch) -> dict:
    seen: dict = {}

    def fake(snapshot, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(
            provider=None, reason="spy", auto=False, wait_until=None
        )

    monkeypatch.setattr(provider_runner, "choose_provider", fake)
    return seen


def test_registry_holds_five_names_in_order(tmp_path: Path):
    assert _router(tmp_path).adapters.names() == [
        "agy", "copilot", "codex", "cursor", "claude"
    ]


def test_disabled_copilot_missing_from_enabled_map(tmp_path: Path, monkeypatch):
    seen = _spy(monkeypatch)
    router = _router(tmp_path, copilot_enabled=False)
    router._choose(_task(), "implement", model_class="worker")
    assert seen["enabled"]["copilot"] is False
    assert seen["enabled"]["codex"] is True
    assert list(seen["enabled"]) == ["agy", "copilot", "codex", "cursor", "claude"]


def test_extra_adapter_is_offered_to_choose_provider(tmp_path: Path, monkeypatch):
    class Sixth(BaseAdapter):
        name = "sixth"

        def enabled(self) -> bool:
            return True

        def implementation_budget(self) -> int:
            return 2

    seen = _spy(monkeypatch)
    router = _router(tmp_path)
    router.adapters.register(Sixth())
    router._choose(_task(), "implement", model_class="worker")
    assert seen["enabled"]["sixth"] is True
    assert router._has_other_candidate(_task(), "implement", "codex", set())


def test_implement_passes_provider_attempt_to_adapter(tmp_path: Path, monkeypatch):
    from harness.adapters.base import CallContext
    from harness.runner import AgentResult

    seen: list[CallContext] = []

    class Fake(BaseAdapter):
        name = "fake"

        def enabled(self) -> bool:
            return True

        def implementation_budget(self) -> int:
            return 2

        def implement(self, ctx: CallContext) -> AgentResult:
            seen.append(ctx)
            return AgentResult(False, "", "task failed", provider="fake")

    router = _router(tmp_path)
    router.adapters.register(Fake())
    monkeypatch.setattr(
        router, "_choose",
        lambda *a, **k: SimpleNamespace(provider="fake", reason="test"),
    )
    task = _task()
    task.model_class = "worker"
    for attempt in (1, 2):
        router._implement_with_routing(
            task, tmp_path, 5, attempt, None,
            env=None, code_map_offered=False, on_process_start=None,
        )
    assert [c.provider_attempt for c in seen] == [0, 1]
    assert [c.attempt for c in seen] == [1, 2]


def test_implement_missing_adapter_is_launch_failure(tmp_path: Path, monkeypatch):
    router = _router(tmp_path)
    monkeypatch.setattr(
        router, "_choose",
        lambda *a, **k: SimpleNamespace(provider="ghost", reason="test"),
    )
    monkeypatch.setattr(router, "_has_other_candidate", lambda *a, **k: False)
    task = _task()
    task.model_class = "worker"
    result = router._implement_with_routing(
        task, tmp_path, 5, 1, None,
        env=None, code_map_offered=False, on_process_start=None,
    )
    assert result.error == "no adapter for ghost"
    assert result.launch_failure


def test_review_calls_chosen_adapter(tmp_path: Path, monkeypatch):
    from harness.runner import AgentResult

    seen: list[tuple] = []

    class Fake(BaseAdapter):
        name = "fake"

        def enabled(self) -> bool:
            return True

        def review(self, *args, **kwargs) -> AgentResult:
            seen.append((*args, kwargs))
            return AgentResult(True, "VERDICT: PASS", provider="fake")

    router = _router(tmp_path)
    router.adapters.register(Fake())
    monkeypatch.setattr(
        router, "_choose",
        lambda *a, **k: SimpleNamespace(provider="fake", reason="test"),
    )
    task = _task()
    result = router._review_with_routing(
        task, tmp_path, "base", "verification ok", 5,
        env={"TEST": "1"}, code_map_offered=True, on_process_start=None,
    )
    assert result.provider == "fake"
    assert seen == [(
        task, tmp_path, "base", "verification ok", 5,
        {"env": {"TEST": "1"}, "code_map_offered": True, "on_process_start": None},
    )]


def test_review_missing_adapter_is_launch_failure(tmp_path: Path, monkeypatch):
    router = _router(tmp_path)
    monkeypatch.setattr(
        router, "_choose",
        lambda *a, **k: SimpleNamespace(provider="ghost", reason="test"),
    )
    monkeypatch.setattr(router, "_has_other_candidate", lambda *a, **k: False)
    result = router._review_with_routing(
        _task(), tmp_path, "base", "verification ok", 5,
        env=None, code_map_offered=False, on_process_start=None,
    )
    assert result.error == "no adapter for ghost"
    assert result.launch_failure


def test_plan_repair_calls_chosen_adapter_once(tmp_path: Path, monkeypatch):
    from harness.runner import AgentResult

    seen: list[tuple] = []

    class Fake(BaseAdapter):
        name = "fake"

        def enabled(self) -> bool:
            return True

        def plan_change(self, *args, **kwargs) -> AgentResult:
            seen.append((*args, kwargs))
            return AgentResult(True, "plan", provider="fake")

    router = _router(tmp_path)
    router.adapters.register(Fake())
    monkeypatch.setattr(
        router, "_choose",
        lambda *a, **k: SimpleNamespace(provider="fake", reason="test"),
    )
    task = _task()
    result = router._plan_change_with_routing(
        task, tmp_path, "failed", 5, model_class="planner", env={"TEST": "1"},
    )
    assert result.provider == "fake"
    assert seen == [(
        task, tmp_path, "failed", 5,
        {"model_class": "planner", "env": {"TEST": "1"}},
    )]


def test_plan_repair_missing_adapter_is_launch_failure(tmp_path: Path, monkeypatch):
    router = _router(tmp_path)
    picks = iter(["ghost", None])
    monkeypatch.setattr(
        router, "_choose",
        lambda *a, **k: SimpleNamespace(provider=next(picks), reason="test"),
    )
    monkeypatch.setattr(router, "_disable_provider", lambda *a, **k: None)
    result = router._plan_change_with_routing(
        _task(), tmp_path, "failed", 5, model_class="planner", env=None,
    )
    assert result.error == "no adapter for ghost"
    assert result.launch_failure
