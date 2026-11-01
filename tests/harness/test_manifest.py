from __future__ import annotations

import json
from pathlib import Path

from harness.config import HarnessConfig
from harness.manifest import RunManifestManager
from harness.models import SprintSpec, TaskSpec
from harness.state import StateStore


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_manifest_records_hashes_and_resume_same_run(tmp_path: Path):
    write(tmp_path / "AGENTS.md", "# agents\n")
    write(tmp_path / "ARCHITECTURE.md", "# architecture\n")
    write(tmp_path / "docs/assignment.md", "# assignment\n")
    write(tmp_path / "tasks/A.md", "# A\n")
    write(tmp_path / "tasks/roadmap.yaml", "project: x\n")
    write(tmp_path / "harness.yaml", """
execution: {}
worktree: {}
cursor: {command: definitely-not-an-agent, models: {worker: null}}
verification: {}
paths: {}
context: {}
doctor: {}
""")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    manager = RunManifestManager(tmp_path, tmp_path / ".harness", state)
    sprint = SprintSpec(
        id="s1",
        name="s1",
        tasks={"A": TaskSpec(id="A", file=Path("tasks/A.md"), sprint="s1", stage=1)},
    )

    # A disabled Codex marker is run-local: a brand-new run probes Codex again.
    state.set_meta("s1.A.provider.codex.disabled", "quota:weekly")

    first = manager.start_or_resume(
        sprint=sprint,
        roadmap_path=tmp_path / "tasks/roadmap.yaml",
        config_path=tmp_path / "harness.yaml",
        config=config,
        integration_branch="main",
        started_from_commit="abc",
    )
    assert state.get_meta("s1.A.provider.codex.disabled") is None
    data = json.loads(first.read_text(encoding="utf-8"))
    assert data["started_from_commit"] == "abc"
    assert data["hashes"]["architecture"]
    assert data["hashes"]["task_files"]

    # Resuming the same active run preserves a prior provider switch.
    state.set_meta("s1.A.provider.codex.disabled", "quota:weekly")
    second = manager.start_or_resume(
        sprint=sprint,
        roadmap_path=tmp_path / "tasks/roadmap.yaml",
        config_path=tmp_path / "harness.yaml",
        config=config,
        integration_branch="main",
        started_from_commit="abc",
    )
    assert second == first
    assert state.get_meta("s1.A.provider.codex.disabled") == "quota:weekly"
    resumed = json.loads(second.read_text(encoding="utf-8"))
    assert len(resumed["resumed_at"]) == 1

    manager.finish("s1", success=True)
    done = json.loads(first.read_text(encoding="utf-8"))
    assert done["status"] == "COMPLETED"
    assert manager.current_path("s1") == first
