from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path

import pytest

from harness.agent_ledger import LedgerStore
from harness.config import HarnessConfig
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.home import harness_home
from harness.models import TaskStatus
import harness.outcomes as outcomes
from harness.roadmap import Roadmap
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.usage import UsageRecorder
from harness.verifier import Verifier
from tests.portable import file_exists_command, yaml_quote


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _init_repo(root: Path) -> tuple[HarnessConfig, Roadmap, StateStore, WorktreeManager, Verifier, FailureStore]:
    root.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    write(root / ".gitignore", ".harness/\n")
    write(root / "AGENTS.md", "# test\n")
    write(
        root / "harness.yaml",
        """
execution:
  max_parallel_agents: 1
  default_max_attempts: 1
  default_timeout_minutes: 1
  watchdog_poll_seconds: 1
  min_free_memory_gb: 2.0
  memory_per_agent_gb: 1.0
cursor:
  command: agent
  output_format: text
  models: {}
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths:
  protected: []
  review_required: []
""",
    )
    check = file_exists_command("out-a.txt")
    write(
        root / "tasks/A.md",
        f"""---
allowed_paths: [out-a.txt]
verification: [{yaml_quote(check)}]
review: false
---
# A
""",
    )
    write(
        root / "tasks/roadmap.yaml",
        f"""
project: test
sprints:
  s1:
    tasks:
      A: {{file: tasks/A.md, stage: 1, depends_on: []}}
    stage_verification:
      "1": [{yaml_quote(check)}]
""",
    )
    run(["git", "add", "."], root)
    run(["git", "commit", "-m", "initial"], root)
    config = HarnessConfig.load(root / "harness.yaml")
    roadmap = Roadmap(root, root / "tasks/roadmap.yaml")
    state = StateStore(root / ".harness/state.json")
    worktrees = WorktreeManager(root, root / ".harness")
    verifier = Verifier(config, root / ".harness/logs", worktrees)
    failures = FailureStore(root / ".harness/failures")
    return config, roadmap, state, worktrees, verifier, failures


class SimpleRunner:
    def __init__(self) -> None:
        self.started = threading.Event()

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.started.set()
        (workspace / "out-a.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


def _full_fake_ledger(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    payload = {
        "agents": {
            f"fake-{index}": {
                "project": f"/other/project-{index}",
                "task_id": f"S9-T{index}",
                "harness_pid": 10_000 + index,
                "estimate_gb": 4.0,
                "started_at": "2026-11-03T00:00:00+00:00",
            }
            for index in range(1, 5)
        },
        "waiting": {},
    }
    (folder / "agents.json").write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def test_harness_home_unset_is_dot_harness(monkeypatch, tmp_path: Path) -> None:
    sentinel = tmp_path / "user-home"
    sentinel.mkdir()
    monkeypatch.delenv("HARNESS_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: sentinel))
    assert harness_home() == sentinel / ".harness"


def test_harness_home_env_override(monkeypatch, tmp_path: Path) -> None:
    override = tmp_path / "override-home"
    monkeypatch.setenv("HARNESS_HOME", str(override))
    assert harness_home() == override


def test_default_scheduler_starts_despite_full_user_home_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "fake-user-home"
    _full_fake_ledger(fake_home / ".harness")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    # HARNESS_HOME remains the conftest temp folder — default LedgerStore must use it.
    assert Path(os.environ["HARNESS_HOME"]) != fake_home / ".harness"
    assert LedgerStore().folder == Path(os.environ["HARNESS_HOME"])

    config, roadmap, state, worktrees, verifier, failures = _init_repo(tmp_path / "project")
    runner = SimpleRunner()
    scheduler = Scheduler(
        tmp_path / "project",
        config,
        state,
        worktrees,
        runner,
        verifier,
        failures=failures,
        roadmap=roadmap,
    )
    thread = threading.Thread(target=lambda: scheduler.run_sprint(roadmap.sprint("s1")))
    thread.start()
    assert runner.started.wait(timeout=30)
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert state.get("A").status == TaskStatus.DONE
    # Fake user-home ledger must stay exactly as planted.
    assert json.loads((fake_home / ".harness" / "agents.json").read_text(encoding="utf-8"))[
        "agents"
    ]


def test_suite_does_not_touch_path_home_harness(tmp_path: Path, monkeypatch) -> None:
    sentinel = tmp_path / "sentinel-home"
    sentinel.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: sentinel))
    # Reset outcomes override so production resolution (harness_home) is used.
    monkeypatch.setattr(outcomes, "_folder_configured", False)
    monkeypatch.setattr(outcomes, "_folder_override", None)

    store = LedgerStore()
    store.register_agent(tmp_path / "project", "probe", os.getpid(), 1.0)
    UsageRecorder(tmp_path / "project" / ".harness" / "usage.jsonl").record(
        {"task_id": "probe", "ok": True}
    )

    assert not (sentinel / ".harness").exists()
    assert (Path(os.environ["HARNESS_HOME"]) / "agents.json").exists()
    assert (Path(os.environ["HARNESS_HOME"]) / "outcomes.jsonl").exists()
