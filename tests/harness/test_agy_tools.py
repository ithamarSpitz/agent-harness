from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from harness.agy_runner import AgyAgentRunner
from harness.config import HarnessConfig
from harness.models import TaskSpec
from harness.runner import AgentResult

from tests.harness.test_agy_runner import _task, _write_config


def _config_with_ui(tmp_path: Path, enabled: bool) -> HarnessConfig:
    _write_config(tmp_path)
    path = tmp_path / "harness.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    payload["ui_review"] = {"enabled": enabled}
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return HarnessConfig.load(path)


def test_agy_runner_does_not_support_ui_review() -> None:
    assert AgyAgentRunner.supports_ui_review is False


@pytest.mark.parametrize("enabled", [True, False])
def test_agy_calls_never_get_playwright_or_mcp_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    runner = AgyAgentRunner(_config_with_ui(tmp_path, enabled), tmp_path / "logs")
    task: TaskSpec = _task(tmp_path)
    calls: list[dict[str, object]] = []

    def fake_invoke(**kwargs: object) -> AgentResult:
        calls.append(kwargs)
        return AgentResult(True, "ok", provider="agy")

    monkeypatch.setattr(runner, "_invoke", fake_invoke)
    runner.implement(task, tmp_path, 1, 1, None, base_sha="abc")
    runner.review(task, tmp_path, "def", "passed", 1)
    for call in calls:
        text = f"{call['prompt']} {call['settings']}".lower()
        assert "playwright" not in text
        assert "mcp" not in text
    command = runner._command("m", "low", "plan", "{}")
    assert "mcp" not in " ".join(command).lower()
