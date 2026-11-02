from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from harness.agent_ledger import LedgerStore


def test_agent_and_waiting_records_round_trip(tmp_path):
    store = LedgerStore(tmp_path, process_alive=lambda _pid: True)

    agent_id = store.register_agent(tmp_path / "project", "S8-T33", 123, 1.25)
    assert agent_id
    store.set_waiting(tmp_path / "project", 123, 3)
    first = store.snapshot()

    assert first.agents[agent_id].task_id == "S8-T33"
    assert first.agents[agent_id].estimate_gb == 1.25
    assert Path(first.agents[agent_id].project).is_absolute()
    waiting = next(iter(first.waiting.values()))
    assert waiting.ready_tasks == 3

    store.set_waiting(tmp_path / "project", 123, 2)
    second = store.snapshot()
    assert next(iter(second.waiting.values())).since == waiting.since
    store.set_waiting(tmp_path / "project", 123, 0)
    store.unregister_agent(agent_id)
    assert store.snapshot().agents == {}
    assert store.snapshot().waiting == {}


def test_dead_process_records_are_removed_on_read(tmp_path):
    writer = LedgerStore(tmp_path, process_alive=lambda _pid: True)
    writer.register_agent(tmp_path, "dead-task", 111, 0.5)
    writer.set_waiting(tmp_path, 111, 2)

    assert LedgerStore(tmp_path, process_alive=lambda _pid: False).snapshot().agents == {}
    raw = json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))
    assert raw == {"agents": {}, "waiting": {}}


def test_corrupt_file_is_replaced_and_warned(tmp_path, caplog):
    path = tmp_path / "agents.json"
    path.write_text("not json", encoding="utf-8")

    ledger = LedgerStore(tmp_path, process_alive=lambda _pid: True).snapshot()

    assert ledger.agents == {}
    assert json.loads(path.read_text(encoding="utf-8")) == {"agents": {}, "waiting": {}}
    assert "corrupt" in caplog.text


def test_register_agent_returns_empty_when_unavailable(tmp_path, caplog):
    # Hold the lock in a sibling process so registration times out without raising.
    holder = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from harness.agent_ledger import LedgerStore\n"
        "store = LedgerStore(Path(sys.argv[1]), process_alive=lambda _pid: True)\n"
        "with store._locked():\n"
        "    time.sleep(2)\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd())
    process = subprocess.Popen([sys.executable, "-c", holder, str(tmp_path)], env=env)
    try:
        deadline = time.monotonic() + 5
        while not (tmp_path / "agents.lock").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.1)
        store = LedgerStore(tmp_path, process_alive=lambda _pid: True, lock_timeout=0.2)
        agent_id = store.register_agent(tmp_path, "miss", 1, 0.1)
        assert agent_id == ""
        assert "unavailable" in caplog.text
        assert not (tmp_path / "agents.json").exists()
    finally:
        process.wait(timeout=10)


def test_two_processes_update_the_ledger_in_parallel(tmp_path):
    # Attribute records to this still-alive parent so a worker exiting mid-run cannot
    # make the peer reap the other's agents via _drop_dead_records (AC1 must prove locking).
    worker = (
        "import sys\n"
        "from pathlib import Path\n"
        "from harness.agent_ledger import LedgerStore\n"
        "folder = Path(sys.argv[1])\n"
        "label = sys.argv[2]\n"
        "harness_pid = int(sys.argv[3])\n"
        "store = LedgerStore(folder)\n"
        "own = []\n"
        "for index in range(40):\n"
        "    agent_id = store.register_agent(Path.cwd(), f'{label}-{index}', harness_pid, 0.1)\n"
        "    assert agent_id, 'register_agent failed to persist'\n"
        "    own.append(agent_id)\n"
        "for agent_id in own[::2]:\n"
        "    store.unregister_agent(agent_id)\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path.cwd())
    parent_pid = os.getpid()
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", worker, str(tmp_path), str(index), str(parent_pid)],
            env=env,
        )
        for index in range(2)
    ]
    for process in processes:
        assert process.wait(timeout=30) == 0

    ledger = LedgerStore(tmp_path).snapshot()
    assert len(ledger.agents) == 40
    assert len({record.task_id for record in ledger.agents.values()}) == 40
    assert all(record.harness_pid == parent_pid for record in ledger.agents.values())
    json.loads((tmp_path / "agents.json").read_text(encoding="utf-8"))
