from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from harness.cli import _cmd_run_live, build_runtime, cmd_release, cmd_rerun_blocker, cmd_status
from harness.config import HarnessConfig
from harness.git_worktree import WorktreeManager
from harness.models import TaskStatus
from harness.roadmap import Roadmap
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier
from tests.portable import fail_command, file_exists_command, make_python_script, shell_join, yaml_quote


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _make_release_recorder(tmp_path: Path) -> tuple[Path, Path]:
    recorder = make_python_script(
        tmp_path.parent / f"{tmp_path.name}-release-recorder.py",
        """import os
import sys
from pathlib import Path

log = Path(sys.argv[1])
marker = sys.argv[2]
print(f"stdout:{marker}", flush=True)
print(f"stderr:{marker}", file=sys.stderr, flush=True)
log.parent.mkdir(parents=True, exist_ok=True)
with log.open("a", encoding="utf-8") as fh:
    fh.write(f"{marker}|{Path(os.getcwd()).resolve()}\\n")
if marker == "fail":
    raise SystemExit(2)
""",
    )
    record = tmp_path.parent / f"{tmp_path.name}-release-record.txt"
    return recorder, record


def _release_command(recorder: Path, record: Path, marker: str) -> str:
    return shell_join([sys.executable, str(recorder), str(record), marker])


def _recorded_calls(record: Path) -> list[tuple[str, Path]]:
    if not record.exists():
        return []
    calls: list[tuple[str, Path]] = []
    for line in record.read_text(encoding="utf-8").splitlines():
        marker, cwd = line.split("|", 1)
        calls.append((marker, Path(cwd)))
    return calls


class FakeRunner:
    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        out = workspace / "out.txt"
        out.write_text(task.id + "\n", encoding="utf-8")
        return AgentResult(True, "ok")

    def review(self, task, workspace, base_ref, verification_summary, timeout_minutes, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n")


def _base_harness_yaml(release_commands: list[str] | None) -> str:
    release_block = ""
    if release_commands is not None:
        quoted = ", ".join(yaml_quote(cmd) for cmd in release_commands)
        release_block = f"""
release:
  commands: [{quoted}]
  timeout_minutes: 1
"""
    return f"""
execution:
  max_parallel_agents: 1
  default_max_attempts: 1
  default_timeout_minutes: 1
  keep_successful_worktrees: false
cursor:
  command: agent
  output_format: text
  models: {{}}
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths:
  protected: []
  review_required: []
{release_block}"""


def _init_sprint_repo(
    tmp_path: Path,
    *,
    release_commands: list[str] | None = None,
    stage_verification: list[str] | None = None,
) -> tuple[Scheduler, Roadmap, StateStore, HarnessConfig]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-b", "main"], tmp_path)
    run(["git", "config", "user.name", "Test"], tmp_path)
    run(["git", "config", "user.email", "test@example.com"], tmp_path)
    write(tmp_path / ".gitignore", ".harness/\n")
    write(tmp_path / "AGENTS.md", "# test\n")
    verify = file_exists_command("out.txt")
    write(
        tmp_path / "tasks/T.md",
        f"""---
allowed_paths: [out.txt]
verification: [{yaml_quote(verify)}]
review: false
---
# T
""",
    )
    stage_cmds = stage_verification if stage_verification is not None else []
    quoted_stage = ", ".join(yaml_quote(cmd) for cmd in stage_cmds)
    write(
        tmp_path / "tasks/roadmap.yaml",
        f"""
project: test
sprints:
  s1:
    tasks:
      T: {{file: tasks/T.md, stage: 1, depends_on: []}}
    stage_verification:
      "1": [{quoted_stage}]
""",
    )
    write(tmp_path / "harness.yaml", _base_harness_yaml(release_commands))
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    config = HarnessConfig.load(tmp_path / "harness.yaml")
    roadmap = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    verifier = Verifier(config, tmp_path / ".harness/logs", worktrees)
    scheduler = Scheduler(tmp_path, config, state, worktrees, FakeRunner(), verifier)
    return scheduler, roadmap, state, config


def _assert_release_skipped(record: Path, state: StateStore) -> None:
    assert _recorded_calls(record) == []
    assert state.get_meta("s1.release.status") is None
    assert not (state.path.parent / "logs" / "s1-release.log").exists()


def test_completed_sprint_runs_release_commands_in_order(tmp_path: Path):
    recorder, record = _make_release_recorder(tmp_path)
    cmd1 = _release_command(recorder, record, "first")
    cmd2 = _release_command(recorder, record, "second")
    scheduler, roadmap, state, _ = _init_sprint_repo(tmp_path, release_commands=[cmd1, cmd2])

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("T").status == TaskStatus.DONE
    calls = _recorded_calls(record)
    assert [marker for marker, _cwd in calls] == ["first", "second"]
    assert all(cwd == tmp_path.resolve() for _marker, cwd in calls)

    release_log = (tmp_path / ".harness/logs/s1-release.log").read_text(encoding="utf-8")
    assert "stdout:first" in release_log
    assert "stderr:first" in release_log
    assert "stdout:second" in release_log
    assert "stderr:second" in release_log
    assert state.get_meta("s1.release.status") == "done"


class FailingRunner(FakeRunner):
    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        return AgentResult(False, "blocked")


class QuotaRunner(FakeRunner):
    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        return AgentResult(False, "usage limit reached", "quota", quota_exhausted=True)


def test_failed_sprint_and_dry_run_skip_release(tmp_path: Path):
    recorder, record = _make_release_recorder(tmp_path)
    marker = _release_command(recorder, record, "ran")

    blocked_root = tmp_path / "blocked"
    scheduler, roadmap, state, _ = _init_sprint_repo(blocked_root, release_commands=[marker])
    scheduler.runner = FailingRunner()
    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    _assert_release_skipped(record, state)

    stage_root = tmp_path / "stage"
    missing = tmp_path / "stage-ok"
    scheduler, roadmap, state, _ = _init_sprint_repo(
        stage_root,
        release_commands=[marker],
        stage_verification=[file_exists_command(missing)],
    )
    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("T").status == TaskStatus.VERIFIED
    _assert_release_skipped(record, state)

    quota_root = tmp_path / "quota"
    scheduler, roadmap, state, _ = _init_sprint_repo(quota_root, release_commands=[marker])
    scheduler.runner = QuotaRunner()
    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("T").status == TaskStatus.WAITING_FOR_QUOTA
    _assert_release_skipped(record, state)

    dry_root = tmp_path / "dry"
    scheduler, roadmap, state, _ = _init_sprint_repo(dry_root, release_commands=[marker])
    assert scheduler.run_sprint(roadmap.sprint("s1"), dry_run=True)
    _assert_release_skipped(record, state)


def test_failing_release_command_stops_rest_and_keeps_merged_code(tmp_path: Path, capsys):
    recorder, record = _make_release_recorder(tmp_path)
    ok_cmd = _release_command(recorder, record, "ok")
    fail_cmd = _release_command(recorder, record, "fail")
    never_cmd = _release_command(recorder, record, "never")
    scheduler, roadmap, state, _config = _init_sprint_repo(
        tmp_path, release_commands=[ok_cmd, fail_cmd, never_cmd]
    )
    initial_head = run(["git", "rev-parse", "HEAD"], tmp_path)

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("T").status == TaskStatus.DONE
    assert state.get_meta("s1.run_status") == "COMPLETED"
    assert state.get_meta("s1.release.status") == "failed"
    assert [marker for marker, _cwd in _recorded_calls(record)] == ["ok", "fail"]
    assert run(["git", "rev-parse", "HEAD"], tmp_path) != initial_head
    assert run(["git", "show", "HEAD:out.txt"], tmp_path) == "T"
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "T\n"

    release_log = (tmp_path / ".harness/logs/s1-release.log").read_text(encoding="utf-8")
    assert "stdout:ok" in release_log
    assert "stdout:fail" in release_log
    assert "stdout:never" not in release_log

    cmd_status(roadmap, state, "s1")
    out = capsys.readouterr().out
    assert "release: FAILED" in out
    assert fail_cmd in out
    assert "exit 2" in out
    assert "harness release s1" in out


def test_manual_release_reruns_and_refuses_incomplete_sprint(tmp_path: Path):
    recorder, record = _make_release_recorder(tmp_path)
    cmd = _release_command(recorder, record, "once")
    fail = fail_command()
    scheduler, roadmap, state, _config = _init_sprint_repo(tmp_path, release_commands=[cmd, fail])

    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get_meta("s1.release.status") == "failed"
    assert [marker for marker, _cwd in _recorded_calls(record)] == ["once"]

    runtime = build_runtime(tmp_path, tmp_path / "tasks/roadmap.yaml", tmp_path / "harness.yaml")
    runtime.state.get("T").status = TaskStatus.VERIFIED
    runtime.state.save()
    assert cmd_release(runtime, "s1") == 2
    assert [marker for marker, _cwd in _recorded_calls(record)] == ["once"]

    runtime.state.get("T").status = TaskStatus.DONE
    runtime.state.save()
    runtime.config.release.commands = [cmd]
    assert cmd_release(runtime, "s1") == 0
    assert runtime.state.get_meta("s1.release.status") == "done"
    assert [marker for marker, _cwd in _recorded_calls(record)] == ["once", "once"]


def test_missing_release_section_leaves_behavior_unchanged(tmp_path: Path):
    scheduler, roadmap, state, config = _init_sprint_repo(tmp_path, release_commands=None)
    assert scheduler.run_sprint(roadmap.sprint("s1"))
    assert not (tmp_path / ".harness/logs/s1-release.log").exists()
    assert state.get_meta("s1.release.status") is None
    assert config.release.commands == []


def test_last_stage_barrier_recovery_runs_release(tmp_path: Path):
    recorder, record = _make_release_recorder(tmp_path)
    cmd = _release_command(recorder, record, "recovered")
    marker = tmp_path.parent / f"{tmp_path.name}-stage-ok"
    scheduler, roadmap, state, _ = _init_sprint_repo(
        tmp_path,
        release_commands=[cmd],
        stage_verification=[file_exists_command(marker)],
    )

    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    assert state.get("T").status == TaskStatus.VERIFIED
    _assert_release_skipped(record, state)

    marker.write_text("ok\n", encoding="utf-8")
    runtime = build_runtime(tmp_path, tmp_path / "tasks/roadmap.yaml", tmp_path / "harness.yaml")
    assert cmd_rerun_blocker(runtime, "s1", None) == 0
    assert runtime.state.get("T").status == TaskStatus.DONE
    assert runtime.state.get_meta("s1.run_status") == "COMPLETED"
    assert runtime.state.get_meta("s1.release.status") == "done"
    assert [marker_name for marker_name, cwd in _recorded_calls(record)] == ["recovered"]
    assert _recorded_calls(record)[0][1] == tmp_path.resolve()

    assert _cmd_run_live(runtime, "s1", runtime.roadmap.sprint("s1")) == 0
    assert [marker_name for marker_name, _cwd in _recorded_calls(record)] == ["recovered"]


def test_resume_runs_release_when_last_stage_recovery_has_not(tmp_path: Path):
    recorder, record = _make_release_recorder(tmp_path)
    cmd = _release_command(recorder, record, "from-resume")
    marker = tmp_path.parent / f"{tmp_path.name}-stage-ok"
    scheduler, roadmap, state, _ = _init_sprint_repo(
        tmp_path,
        release_commands=[cmd],
        stage_verification=[file_exists_command(marker)],
    )

    assert not scheduler.run_sprint(roadmap.sprint("s1"))
    _assert_release_skipped(record, state)

    marker.write_text("ok\n", encoding="utf-8")
    scheduler.config.release.commands = []
    assert scheduler.rerun_stage_barrier(roadmap.sprint("s1"), 1)
    assert state.get("T").status == TaskStatus.DONE
    _assert_release_skipped(record, state)

    runtime = build_runtime(tmp_path, tmp_path / "tasks/roadmap.yaml", tmp_path / "harness.yaml")
    assert _cmd_run_live(runtime, "s1", runtime.roadmap.sprint("s1")) == 0
    assert runtime.state.get_meta("s1.run_status") == "COMPLETED"
    assert runtime.state.get_meta("s1.release.status") == "done"
    calls = _recorded_calls(record)
    assert [marker_name for marker_name, _cwd in calls] == ["from-resume"]
    assert calls[0][1] == tmp_path.resolve()
    assert "stdout:from-resume" in (tmp_path / ".harness/logs/s1-release.log").read_text(encoding="utf-8")
