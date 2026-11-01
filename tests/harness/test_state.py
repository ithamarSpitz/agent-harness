from pathlib import Path

from harness.models import TaskStatus
from harness.state import StateStore


def test_state_round_trip(tmp_path: Path):
    path = tmp_path / "state.json"
    state = StateStore(path)
    runtime = state.get("T1")
    runtime.status = TaskStatus.BLOCKED
    runtime.attempt = 2
    runtime.last_error = "boom"
    state.save()

    loaded = StateStore(path)
    assert loaded.get("T1").status == TaskStatus.BLOCKED
    assert loaded.get("T1").attempt == 2
    assert loaded.get("T1").last_error == "boom"
