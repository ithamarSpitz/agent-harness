from __future__ import annotations

from pathlib import Path

import yaml

from harness.config import HarnessConfig
from harness.headroom import Headroom
from harness.models import TaskSpec, TaskStatus
from harness.provider_runner import ProviderAgentRunner
from harness.runner import AgentResult
from harness.state import StateStore


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _Codex:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir


class _Cursor:
    def __init__(self) -> None:
        self.models: list[str | None] = []

    def implement(self, *_args, **kwargs) -> AgentResult:
        model = kwargs.get("model_class_override")
        self.models.append(model)
        if model == "auto" and self.models.count("auto") == 1:
            return AgentResult(
                False,
                "",
                "You've hit your usage limit",
                provider="cursor",
                quota_exhausted=True,
                quota_scope="billing",
            )
        return AgentResult(True, "ok", provider="cursor")


def _config(tmp_path: Path) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "cursor": {
                    "enabled": True,
                    "auto_when_exhausted": True,
                    "models": {"worker": "named-cursor-model"},
                },
                "codex": {"enabled": False},
                "providers": {"priority": ["cursor"]},
                "retry": {"sequence": ["worker"]},
            }
        ),
        encoding="utf-8",
    )
    return HarnessConfig.load(path)


def _task(tmp_path: Path) -> TaskSpec:
    brief = tmp_path / "task.md"
    brief.write_text("# task\n", encoding="utf-8")
    return TaskSpec(
        id="T1",
        file=brief,
        sprint="S1",
        stage=1,
        review=False,
        model_class="worker",
        allowed_paths=["**"],
    )


def _setup(tmp_path: Path, clock: _Clock, reset: list[float | None]):
    state = StateStore(tmp_path / "state.json")
    cursor = _Cursor()

    def headroom():
        return {
            "cursor": [
                Headroom("cursor", 100, reset[0], "composer_grok", True),
                Headroom("cursor", 100, reset[0], "other_models", True),
            ]
        }

    runner = ProviderAgentRunner(
        _config(tmp_path),
        state,
        _Codex(tmp_path / "logs"),  # type: ignore[arg-type]
        cursor,  # type: ignore[arg-type]
        headroom_reader=headroom,
        clock=clock,
    )
    task = _task(tmp_path)
    state.get(task.id)
    return runner, state, cursor, task


def test_auto_quota_is_remembered_across_quota_wait(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    reset_at = now + 22 * 24 * 60 * 60
    runner, state, cursor, task = _setup(tmp_path, _Clock(now), [reset_at])

    first = runner.implement(task, tmp_path, 1, 1, None)
    assert first.quota_exhausted
    assert cursor.models == ["auto"]
    assert state.get_meta("cursor.auto.exhausted_until") == reset_at

    runtime = state.get(task.id)
    runtime.status = TaskStatus.WAITING_QUOTA
    runtime.waiting_phase = "implement"
    runtime.waiting_provider = "cursor"
    runtime.last_error = "cursor usage quota exhausted during implement"
    state.set_meta("S1.T1.provider.cursor.disabled", "quota:billing")
    state.save()

    second = runner.implement(task, tmp_path, 1, 1, None)

    assert not second.ok
    assert cursor.models == ["auto"]
    log = (tmp_path / "logs" / "T1-routing.log").read_text(encoding="utf-8")
    assert "cursor auto: out until 2027-02-06" in log


def test_auto_is_offered_after_stored_time_passes(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    reset_at = now + 100
    clock = _Clock(now)
    runner, state, cursor, task = _setup(tmp_path, clock, [reset_at])
    state.set_meta("cursor.auto.exhausted_until", reset_at)

    assert runner.active_provider(task) == "none"
    clock.now = reset_at + 1

    result = runner.implement(task, tmp_path, 1, 1, None)

    assert result.quota_exhausted
    assert cursor.models == ["auto"]


def test_auto_quota_without_known_reset_uses_24_hours(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    runner, state, cursor, task = _setup(tmp_path, _Clock(now), [None])

    result = runner.implement(task, tmp_path, 1, 1, None)

    assert result.quota_exhausted
    assert cursor.models == ["auto"]
    assert state.get_meta("cursor.auto.exhausted_until") == now + 24 * 60 * 60


def test_new_cursor_billing_reset_clears_auto_memory_early(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    stored_until = now + 22 * 24 * 60 * 60
    reset = [stored_until]
    runner, state, cursor, task = _setup(tmp_path, _Clock(now), reset)
    state.set_meta("cursor.auto.exhausted_until", stored_until)
    assert runner.active_provider(task) == "none"

    reset[0] = stored_until + 30 * 24 * 60 * 60
    result = runner.implement(task, tmp_path, 1, 1, None)

    assert result.quota_exhausted
    assert cursor.models == ["auto"]


def test_named_cursor_override_is_unaffected(tmp_path: Path) -> None:
    now = 1_800_000_000.0
    runner, state, cursor, task = _setup(tmp_path, _Clock(now), [now + 1000])
    state.set_meta("cursor.auto.exhausted_until", now + 1000)

    result = runner.implement(
        task, tmp_path, 1, 1, None, model_class_override="worker"
    )

    assert result.ok
    assert cursor.models == ["worker"]
