import json
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


def test_legacy_task_statuses_load_with_current_names(tmp_path: Path):
    path = tmp_path / "state.json"
    legacy = {
        "PENDING": TaskStatus.PENDING,
        "READY": TaskStatus.READY,
        "RUNNING": TaskStatus.IMPLEMENTING,
        "VERIFYING": TaskStatus.CHECKING,
        "REVIEWING": TaskStatus.REVIEWING,
        "VERIFIED": TaskStatus.PASSED,
        "DONE": TaskStatus.DONE,
        "RETRY": TaskStatus.RETRY,
        "BLOCKED": TaskStatus.BLOCKED,
        "WAITING_FOR_QUOTA": TaskStatus.WAITING_QUOTA,
        "WAITING_FOR_CAPACITY": TaskStatus.WAITING_PROVIDER_ERROR,
    }
    path.write_text(json.dumps({"tasks": {name: {"status": name} for name in legacy}}), encoding="utf-8")

    state = StateStore(path)

    assert {name: state.get(name).status for name in legacy} == legacy
