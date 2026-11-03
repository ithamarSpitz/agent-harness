from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness.config import ExecutionConfig, HarnessConfig
from harness.git_worktree import WorktreeManager
from harness.models import TaskSpec
from harness.verifier import Verifier
from tests.portable import shell_join


def _write_harness_yaml(root: Path, extra_execution: str = "") -> None:
    execution = "execution:\n"
    if extra_execution:
        execution += extra_execution
    else:
        execution = "execution: {}\n"
    (root / "harness.yaml").write_text(
        f"""
{execution}
worktree: {{}}
cursor: {{models: {{}}}}
verification:
  global_task_commands: []
  stage_commands: []
paths: {{protected: [], review_required: []}}
""",
        encoding="utf-8",
    )


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
        ).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_verification_timeout_minutes_default_and_positive_only(tmp_path: Path) -> None:
    _write_harness_yaml(tmp_path)
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    assert config.execution.verification_timeout_minutes == 30.0
    assert ExecutionConfig().verification_timeout_minutes == 30.0

    _write_harness_yaml(tmp_path, "  verification_timeout_minutes: 0\n")
    with pytest.raises(ValueError, match="verification_timeout_minutes"):
        HarnessConfig.load(tmp_path / "harness.yaml")

    _write_harness_yaml(tmp_path, "  verification_timeout_minutes: -1\n")
    with pytest.raises(ValueError, match="verification_timeout_minutes"):
        HarnessConfig.load(tmp_path / "harness.yaml")


def test_hanging_verification_command_times_out_with_partial_output_and_kills_children(
    tmp_path: Path,
) -> None:
    _write_harness_yaml(tmp_path, "  verification_timeout_minutes: 0.05\n")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    verifier = Verifier(config, tmp_path / "logs", WorktreeManager(tmp_path, tmp_path / ".harness"))

    pid_file = tmp_path / "child.pid"
    hang_script = tmp_path / "hang_parent.py"
    hang_script.write_text(
        "import subprocess, sys, time\n"
        "print('hang-marker', flush=True)\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pid_file)!r}, 'w', encoding='utf-8').write(str(child.pid))\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    hang_command = shell_join([sys.executable, str(hang_script)])

    result = verifier.verify_stage(tmp_path, [hang_command], "stage-timeout")

    assert not result.ok
    failure = result.failures[0]
    assert failure.startswith("timed out after 0.05 minutes:")
    assert hang_command in failure
    assert "hang-marker" in result.summary

    deadline = time.time() + 20
    while not (pid_file.exists() and pid_file.read_text(encoding="utf-8").strip()) and time.time() < deadline:
        time.sleep(0.1)
    assert pid_file.exists() and pid_file.read_text(encoding="utf-8").strip(), (
        "parent should have recorded the child pid before the timeout"
    )
    child_pid = int(pid_file.read_text(encoding="utf-8").strip())
    alive_deadline = time.time() + 10
    while _pid_alive(child_pid) and time.time() < alive_deadline:
        time.sleep(0.2)
    assert not _pid_alive(child_pid)


def test_normal_verification_command_still_succeeds(tmp_path: Path) -> None:
    _write_harness_yaml(tmp_path)
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    verifier = Verifier(config, tmp_path / "logs", WorktreeManager(tmp_path, tmp_path / ".harness"))

    ok_script = tmp_path / "ok.py"
    ok_script.write_text("print('ok-marker')\n", encoding="utf-8")
    ok_command = shell_join([sys.executable, str(ok_script)])
    stage = verifier.verify_stage(tmp_path, [ok_command], "stage-ok")
    assert stage.ok
    assert "ok-marker" in stage.summary
    assert not stage.failures

    task = TaskSpec(
        id="T1",
        file=Path("tasks/T1.md"),
        sprint="s",
        stage=1,
        verification=[ok_command],
        allowed_paths=[],
    )
    # Avoid requiring a git repo: integrated revalidation only runs commands.
    integrated = verifier.verify_integrated_task(task, tmp_path)
    assert integrated.ok
    assert "ok-marker" in integrated.summary

    ok, summary, failed, code = verifier.run_shell_commands(
        [ok_command],
        tmp_path,
        timeout_seconds=30,
        log_name="shell-ok.log",
    )
    assert ok
    assert failed is None
    assert code is None
    assert "ok-marker" in summary


def test_run_shell_commands_timeout_uses_shared_helper(tmp_path: Path) -> None:
    _write_harness_yaml(tmp_path)
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    verifier = Verifier(config, tmp_path / "logs", WorktreeManager(tmp_path, tmp_path / ".harness"))

    hang_script = tmp_path / "shell_hang.py"
    hang_script.write_text(
        "print('partial-shell', flush=True)\n"
        "import time\n"
        "time.sleep(120)\n",
        encoding="utf-8",
    )
    hang = shell_join([sys.executable, str(hang_script)])
    ok, summary, failed, code = verifier.run_shell_commands(
        [hang],
        tmp_path,
        timeout_seconds=2,
        log_name="shell-timeout.log",
    )
    assert not ok
    assert failed == hang
    assert code == "timeout"
    assert "timed out after" in summary
    assert "partial-shell" in summary
