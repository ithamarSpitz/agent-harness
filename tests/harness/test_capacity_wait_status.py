from __future__ import annotations

from pathlib import Path

from harness.capacity import CapacityManager
from harness.config import HarnessConfig
from harness.models import TaskStatus
from harness.state import StateStore


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _manager(tmp_path: Path) -> tuple[CapacityManager, StateStore]:
    _write(
        tmp_path / "harness.yaml",
        """
execution: {}
cursor:
  models: {worker: null, reviewer: null}
capacity:
  keep_awake: false
  retry_interval_minutes: 0.01
verification: {}
paths: {}
""",
    )
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    return CapacityManager(config, state), state


def test_wait_once_keeps_recorded_pending_when_no_waiting_task(tmp_path: Path):
    manager, state = _manager(tmp_path)
    state.get("A").status = TaskStatus.PENDING
    state.save()
    state.set_meta("s1.run_status", TaskStatus.PENDING.value)

    manager.wait_once(sprint_id="s1", sleep_fn=lambda _seconds: None)

    assert state.get_meta("s1.run_status") == TaskStatus.PENDING.value


def test_wait_once_sets_run_status_from_waiting_task(tmp_path: Path):
    manager, state = _manager(tmp_path)
    state.get("A").status = TaskStatus.WAITING_NETWORK
    state.save()

    manager.wait_once(sprint_id="s1", sleep_fn=lambda _seconds: None)

    assert state.get_meta("s1.run_status") == TaskStatus.WAITING_NETWORK.value


def test_wait_once_defaults_to_provider_error_when_nothing_recorded(tmp_path: Path):
    manager, state = _manager(tmp_path)
    state.get("A").status = TaskStatus.PENDING
    state.save()

    manager.wait_once(sprint_id="s1", sleep_fn=lambda _seconds: None)

    assert state.get_meta("s1.run_status") == TaskStatus.WAITING_PROVIDER_ERROR.value
