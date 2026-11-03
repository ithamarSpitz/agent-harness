from __future__ import annotations

from pathlib import Path

import yaml

from harness.config import HarnessConfig
from harness.models import TaskSpec, TaskStatus
from harness.provider_runner import ProviderAgentRunner
from harness.runner import AgentResult
from harness.state import StateStore
from harness.usage import UsageRecorder


def _task(tmp_path: Path) -> TaskSpec:
    task_file = tmp_path / "task.md"
    task_file.write_text("# task\n", encoding="utf-8")
    return TaskSpec(id="T1", file=task_file, sprint="S1", stage=1, review=True, allowed_paths=["**"])


def _write_config(tmp_path: Path) -> HarnessConfig:
    payload = {
        "execution": {
            "max_parallel_agents": 1,
            "default_max_attempts": 2,
            "default_timeout_minutes": 1,
            "stall_timeout_minutes": 5,
            "watchdog_poll_seconds": 0.1,
        },
        "cursor": {
            "enabled": True,
            "command": "agent",
            "models": {
                "worker": "composer-2.5",
                "hard_worker": "cursor-grok-4.6-high",
                "reviewer": "cursor-grok-4.6-high",
                "escalation": "claude-opus-5-thinking-high",
            },
        },
        "providers": {"priority": ["codex", "cursor"]},
        "codex": {
            "enabled": True,
            "command": "codex",
            "reasoning_effort": "high",
            "sandbox": "workspace-write",
            "implementation_sequence": [
                {"model": "gpt-5.6-sol", "reasoning_effort": "medium"},
            ],
            "review_model": "gpt-5.6-sol",
            "planner_model": "gpt-5.6-sol",
        },
        "retry": {"sequence": ["worker"]},
        "verification": {"global_task_commands": ["git diff --check"], "stage_commands": []},
        "paths": {"protected": [], "review_required": []},
    }
    path = tmp_path / "harness.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return HarnessConfig.load(path)


class _FakeCodex:
    def __init__(self, results: list[AgentResult] | None = None, available: bool = True):
        self.results = list(results or [])
        self.available = available
        self.models: list[str] = []

    def availability(self, **kwargs):
        return self.available, "ok" if self.available else "not logged in"

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, *, model, reasoning_effort=None, env=None, **kwargs):
        self.models.append(model)
        if self.results:
            return self.results.pop(0)
        return AgentResult(True, "ok", model=model, provider="codex")

    def review(self, *args, model, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n", model=model, provider="codex")

    def plan_change(self, *args, model, **kwargs):
        return AgentResult(True, "CHANGE_KIND: NONE\n", model=model, provider="codex")


class _FakeCursor:
    def __init__(self, results: list[AgentResult] | None = None):
        self.results = list(results or [])
        self.implement_classes: list[str | None] = []

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.implement_classes.append(kwargs.get("model_class_override"))
        if self.results:
            return self.results.pop(0)
        return AgentResult(True, "ok", model="cursor", provider="cursor")

    def review(self, *args, **kwargs):
        if self.results:
            return self.results.pop(0)
        return AgentResult(True, "VERDICT: PASS\n", model="cursor-review", provider="cursor")

    def plan_change(self, *args, **kwargs):
        return AgentResult(True, "CHANGE_KIND: NONE\n", provider="cursor")


def _router(tmp_path: Path, codex: _FakeCodex, cursor: _FakeCursor):
    config = _write_config(tmp_path)
    state = StateStore(tmp_path / "state.json")
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    return ProviderAgentRunner(config, state, codex, cursor, usage), state, usage


def test_quota_disable_cleared_on_waiting_quota_resume_tries_codex_again(tmp_path: Path, capsys):
    """Codex quota → Cursor quota wait → resume must probe Codex again once it has quota."""
    codex = _FakeCodex([
        AgentResult(False, "", "limit", quota_exhausted=True, provider="codex"),
        AgentResult(True, "codex ok after refill", provider="codex"),
    ])
    cursor = _FakeCursor([
        AgentResult(False, "", "cursor limit", quota_exhausted=True, provider="cursor"),
    ])
    router, state, _ = _router(tmp_path, codex, cursor)
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    first = router.implement(task, workspace, 1, 1, None)
    assert not first.ok
    assert first.provider == "cursor"
    assert first.quota_exhausted
    assert state.get_meta("S1.T1.provider.codex.disabled") == "quota:unspecified"
    assert router.active_provider(task) == "cursor"

    # Scheduler would pause here; simulate WAITING_QUOTA then resume budget check.
    runtime = state.get(task.id)
    runtime.status = TaskStatus.WAITING_QUOTA
    runtime.waiting_phase = "implement"
    runtime.waiting_provider = "cursor"
    runtime.last_error = "cursor usage quota exhausted during implement"
    state.save()

    assert router.has_implementation_budget(task)
    assert state.get_meta("S1.T1.provider.codex.disabled") is None
    assert router.active_provider(task) == "codex"
    logged = capsys.readouterr().out
    assert "codex re-enabled for T1 (was: quota:unspecified)" in logged

    # Mimic scheduler clearing wait fields before the implement call.
    runtime = state.get(task.id)
    runtime.status = TaskStatus.IMPLEMENTING
    runtime.waiting_phase = None
    runtime.waiting_provider = None
    runtime.last_error = None
    state.save()

    resumed = router.implement(task, workspace, 1, 1, None)
    assert resumed.ok
    assert resumed.provider == "codex"
    assert codex.models == ["gpt-5.6-sol", "gpt-5.6-sol"]
    assert cursor.implement_classes == ["worker"]


def test_auth_disable_not_cleared_by_quota_resume(tmp_path: Path, capsys):
    codex = _FakeCodex([AgentResult(True, "should not run", provider="codex")])
    cursor = _FakeCursor([AgentResult(True, "cursor ok", provider="cursor")])
    router, state, _ = _router(tmp_path, codex, cursor)
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    state.set_meta("S1.T1.provider.codex.disabled", "auth_or_cli_unavailable")
    runtime = state.get(task.id)
    runtime.status = TaskStatus.WAITING_QUOTA
    runtime.waiting_phase = "implement"
    runtime.waiting_provider = "cursor"
    runtime.last_error = "cursor usage quota exhausted during implement"
    state.save()

    assert router.has_implementation_budget(task)
    assert state.get_meta("S1.T1.provider.codex.disabled") == "auth_or_cli_unavailable"
    assert router.active_provider(task) == "cursor"
    assert "re-enabled" not in capsys.readouterr().out

    runtime = state.get(task.id)
    runtime.status = TaskStatus.IMPLEMENTING
    runtime.waiting_phase = None
    runtime.waiting_provider = None
    runtime.last_error = None
    state.save()

    result = router.implement(task, workspace, 1, 1, None)
    assert result.ok and result.provider == "cursor"
    assert codex.models == []
    assert cursor.implement_classes == ["worker"]
    assert state.get_meta("S1.T1.provider.codex.disabled") == "auth_or_cli_unavailable"


def test_quota_phase_disable_cleared_on_review_resume_via_last_error(tmp_path: Path, capsys):
    codex = _FakeCodex([AgentResult(True, "VERDICT: PASS\n", provider="codex")])
    cursor = _FakeCursor()
    router, state, _ = _router(tmp_path, codex, cursor)
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    state.set_meta("S1.T1.provider.codex.disabled", "quota:5h")
    state.set_meta("S1.T1.provider.codex.disabled.review", "quota:weekly")
    runtime = state.get(task.id)
    # Scheduler skip-verification resume: status flipped, wait error retained.
    runtime.status = TaskStatus.REVIEWING
    runtime.waiting_phase = "review"
    runtime.waiting_provider = "cursor"
    runtime.last_error = "cursor usage quota exhausted during review"
    state.save()

    result = router.review(task, workspace, "base", "verification ok", 1)

    assert result.ok and result.provider == "codex"
    assert state.get_meta("S1.T1.provider.codex.disabled") is None
    assert state.get_meta("S1.T1.provider.codex.disabled.review") is None
    logged = capsys.readouterr().out
    assert "codex re-enabled for T1 (was: quota:5h)" in logged
    assert "codex re-enabled for T1 (was: quota:weekly)" in logged
