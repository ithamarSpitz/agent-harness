from __future__ import annotations

import subprocess
from pathlib import Path

from harness.cli import build_runtime, cmd_cleanup, cmd_reset_task, cmd_unblock
from harness.doctor import Doctor
from harness.failure import FailureRecord
from harness.models import TaskStatus
from tests.portable import make_python_script, yaml_quote


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def init_project(tmp_path: Path) -> Path:
    run(["git", "init", "-b", "main"], tmp_path)
    run(["git", "config", "user.name", "Test"], tmp_path)
    run(["git", "config", "user.email", "test@example.com"], tmp_path)
    fake = make_python_script(tmp_path / "fake-agent.py", """import sys
if 'models' in sys.argv:
    print('cheap-model\\nreview-model')
elif '--version' in sys.argv:
    print('fake 1.0')
""")
    write(tmp_path / ".gitignore", ".harness/\nfake-agent.py\n")
    write(tmp_path / "AGENTS.md", "# agents\n")
    write(tmp_path / "harness.yaml", f"""
execution: {{}}
worktree: {{}}
cursor:
  command: {yaml_quote(fake)}
  models: {{worker: cheap-model, reviewer: review-model}}
verification: {{}}
paths: {{}}
context: {{}}
doctor:
  required_commands: [git]
  required_env: [HARNESS_TEST_ENV]
  min_free_disk_gb: 0
  check_cursor_models: true
  minimum_versions: {{git: "0.0.1"}}
""")
    write(tmp_path / "tasks/A.md", """---
id: A
model_class: worker
allowed_paths: [out.txt]
verification: [echo ok]
---
# A
""")
    write(tmp_path / "tasks/roadmap.yaml", """
project: x
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)
    return fake


def test_doctor_full_checks_auth_models_env_and_recovery_commands(tmp_path: Path, monkeypatch):
    init_project(tmp_path)
    monkeypatch.setenv("HARNESS_TEST_ENV", "yes")
    runtime = build_runtime(tmp_path, tmp_path / "tasks/roadmap.yaml", tmp_path / "harness.yaml")
    checks = Doctor(tmp_path, runtime.config, runtime.worktrees).run(full=True)
    by_name = {check.name: check for check in checks}
    assert by_name["repo-clean"].ok
    assert by_name["cursor-models/auth"].ok
    assert by_name["model:worker"].ok
    assert by_name["model:reviewer"].ok
    assert by_name["env:HARNESS_TEST_ENV"].ok
    assert by_name["version:git"].ok

    rt = runtime.state.get("A")
    rt.status = TaskStatus.BLOCKED
    rt.attempt = 2
    runtime.state.save()
    assert cmd_unblock(runtime.state, "A") == 0
    assert runtime.state.get("A").status == TaskStatus.RETRY
    assert runtime.state.get("A").attempt == 0

    branch, worktree = runtime.worktrees.create("A", runtime.worktrees.head_commit())
    stale = tmp_path / ".harness/worktrees/not-registered"
    stale.mkdir(parents=True)
    assert cmd_cleanup(runtime, stale_worktrees=True, completed=False) == 0
    assert not stale.exists()
    assert worktree.exists()  # registered/live worktrees are never treated as stale

    rt = runtime.state.get("A")
    rt.status = TaskStatus.BLOCKED
    rt.branch = branch
    rt.worktree = str(worktree)
    runtime.state.save()
    runtime.failures.record(
        FailureRecord(
            sprint="s1",
            kind="TASK_BLOCKED",
            message="A blocked",
            stage=1,
            task_ids=["A"],
        )
    )
    assert cmd_reset_task(runtime, "A") == 0
    assert runtime.state.get("A").status == TaskStatus.PENDING
    assert runtime.failures.get("s1") is None
    assert not worktree.exists()

