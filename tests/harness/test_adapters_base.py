from __future__ import annotations

import pytest

from harness.adapters import AdapterRegistry, BaseAdapter, Fault, command_version, classify_result
from harness.agent_runner import AgentResult
from harness.runner import AgentResult as RunnerAgentResult


@pytest.mark.parametrize(
    ("field", "fault"),
    [
        ("quota_exhausted", Fault.QUOTA),
        ("auth_failure", Fault.AUTH),
        ("model_unavailable", Fault.MODEL_UNAVAILABLE),
        ("config_fault", Fault.CONFIG),
        ("capacity_exhausted", Fault.CAPACITY),
        ("transient_error", Fault.TRANSIENT),
    ],
)
def test_classify_result_flags(field: str, fault: Fault) -> None:
    assert classify_result(AgentResult(ok=False, output="", **{field: True})) is fault


def test_classify_result_launch_and_waiting_flags() -> None:
    assert classify_result(RunnerAgentResult(ok=False, output="", launch_failure=True)) is Fault.LAUNCH
    assert classify_result(AgentResult(ok=False, output="", waiting_cause="busy")) is Fault.TRANSIENT
    assert classify_result(AgentResult(ok=False, output="")) is Fault.TASK
    assert classify_result(AgentResult(ok=True, output="", quota_exhausted=True)) is Fault.OK


def test_classify_result_precedence() -> None:
    assert (
        classify_result(RunnerAgentResult(ok=False, output="", quota_exhausted=True, launch_failure=True))
        is Fault.QUOTA
    )


class FakeAdapter(BaseAdapter):
    def __init__(self, name: str, is_enabled: bool) -> None:
        self.name = name
        self._is_enabled = is_enabled

    def enabled(self) -> bool:
        return self._is_enabled


def test_adapter_registry() -> None:
    first = FakeAdapter("first", True)
    second = FakeAdapter("second", False)
    registry = AdapterRegistry()
    registry.register(first)
    registry.register(second)

    assert registry.names() == ["first", "second"]
    assert registry.get("first") is first
    assert list(registry) == [first, second]
    assert registry.enabled() == [first]
    with pytest.raises(ValueError):
        registry.register(FakeAdapter("first", True))


def test_command_version_missing_command_does_not_raise() -> None:
    result = command_version("definitely-not-an-installed-agent-harness-command")
    assert result.installed is False
    assert result.version is None
