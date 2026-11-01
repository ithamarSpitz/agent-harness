from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

from harness.cli import cmd_run
from harness.config import HarnessConfig
from harness.git_worktree import WorktreeManager
from harness.runner import CursorAgentRunner
from tests.portable import make_python_script, yaml_quote


def run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_cursor_trust_only_for_verified_harness_worktree(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-b", "main"], repo)
    run(["git", "config", "user.name", "Test"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    write(repo / ".gitignore", ".harness/\n")
    write(repo / "seed.txt", "seed\n")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "initial"], repo)

    fake = make_python_script(
        tmp_path / "fake-agent.py",
        """import json, os, sys
with open(os.environ['ARG_LOG'], 'w', encoding='utf-8') as fh:
    json.dump(sys.argv[1:], fh)
print('OK')
""",
    )

    config_path = repo / "harness.yaml"
    write(
        config_path,
        f"""
execution:
  stall_timeout_minutes: 0
  watchdog_poll_seconds: 0.01
worktree: {{}}
cursor:
  command: {yaml_quote(fake)}
  output_format: text
  trust_harness_worktrees: true
  models: {{worker: cheap}}
verification: {{}}
paths: {{}}
context: {{}}
doctor: {{}}
""",
    )
    config = HarnessConfig.load(config_path)
    manager = WorktreeManager(repo, repo / ".harness")
    branch, worktree = manager.create("T1", "HEAD")
    runner = CursorAgentRunner(
        config,
        repo / ".harness/logs",
        repo_root=repo,
        worktree_root=manager.worktree_root,
    )

    env = os.environ.copy()
    task_log = tmp_path / "task-args.json"
    env["ARG_LOG"] = str(task_log)
    result = runner._invoke(
        task_id="T1",
        phase="implement",
        prompt="x",
        workspace=worktree,
        model_class="worker",
        timeout_minutes=1,
        log_name="trusted.log",
        env=env,
    )
    assert result.ok
    task_args = json.loads(task_log.read_text(encoding="utf-8"))
    assert "--trust" in task_args
    assert "--force" in task_args

    root_log = tmp_path / "root-args.json"
    env["ARG_LOG"] = str(root_log)
    result = runner._invoke(
        task_id="ROOT",
        phase="implement",
        prompt="x",
        workspace=repo,
        model_class="worker",
        timeout_minutes=1,
        log_name="untrusted-root.log",
        env=env,
    )
    assert result.ok
    root_args = json.loads(root_log.read_text(encoding="utf-8"))
    assert "--trust" not in root_args
    assert "--force" not in root_args

    review_log = tmp_path / "review-args.json"
    env["ARG_LOG"] = str(review_log)
    result = runner._invoke(
        task_id="T1",
        phase="review",
        prompt="x",
        workspace=worktree,
        model_class="worker",
        timeout_minutes=1,
        log_name="review.log",
        mode="ask",
        env=env,
    )
    assert result.ok
    review_args = json.loads(review_log.read_text(encoding="utf-8"))
    assert "--trust" in review_args
    assert "--force" not in review_args

    manager.remove(worktree, branch)


def test_cmd_run_holds_keep_awake_for_live_run(monkeypatch):
    events: list[str] = []

    class FakeKeepAwake:
        def __init__(self, enabled: bool):
            assert enabled is True

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, exc_type, exc, tb):
            events.append("exit")

    class Report:
        ok = True

        @staticmethod
        def render() -> str:
            return "OK"

    runtime = SimpleNamespace(
        validator=SimpleNamespace(validate=lambda sprint_id: Report()),
        worktrees=SimpleNamespace(ensure_repo=lambda: None, is_clean=lambda: True),
        plan_changes=SimpleNamespace(
            ensure_baseline=lambda roadmap: None,
            open_request_id=lambda: None,
        ),
        roadmap=SimpleNamespace(sprint=lambda sprint_id: SimpleNamespace(id=sprint_id, tasks={})),
        failures=SimpleNamespace(record=lambda record: None),
        config=SimpleNamespace(
            execution=SimpleNamespace(keep_awake_during_run=True),
        ),
    )

    import harness.cli as cli

    monkeypatch.setattr(cli, "KeepAwake", FakeKeepAwake)

    def fake_live(rt, sprint_id, sprint):
        assert events == ["enter"]
        return 17

    monkeypatch.setattr(cli, "_cmd_run_live", fake_live)
    assert cmd_run(runtime, "s1", dry_run=False) == 17
    assert events == ["enter", "exit"]


def test_dry_run_does_not_request_keep_awake(monkeypatch):
    class Report:
        ok = True

        @staticmethod
        def render() -> str:
            return "OK"

    class ExplodingKeepAwake:
        def __init__(self, enabled: bool):
            raise AssertionError("dry-run must not acquire keep-awake")

    runtime = SimpleNamespace(
        validator=SimpleNamespace(validate=lambda sprint_id: Report()),
        worktrees=SimpleNamespace(ensure_repo=lambda: None, is_clean=lambda: True),
        plan_changes=SimpleNamespace(
            ensure_baseline=lambda roadmap: None,
            open_request_id=lambda: None,
        ),
        roadmap=SimpleNamespace(sprint=lambda sprint_id: SimpleNamespace(id=sprint_id, tasks={})),
        scheduler=SimpleNamespace(run_sprint=lambda sprint, dry_run: True),
        failures=SimpleNamespace(record=lambda record: None),
        config=SimpleNamespace(execution=SimpleNamespace(keep_awake_during_run=True)),
    )

    import harness.cli as cli

    monkeypatch.setattr(cli, "KeepAwake", ExplodingKeepAwake)
    assert cmd_run(runtime, "s1", dry_run=True) == 0


def test_keep_awake_nested_context_releases_only_outermost(monkeypatch):
    import harness.quota as quota

    calls: list[int] = []

    class Kernel32:
        @staticmethod
        def SetThreadExecutionState(value: int) -> int:
            calls.append(value)
            return 1

    monkeypatch.setattr(quota.os, "name", "nt")
    monkeypatch.setattr(quota.ctypes, "windll", SimpleNamespace(kernel32=Kernel32()), raising=False)
    quota.KeepAwake._depth = 0
    quota.KeepAwake._windows_active = False

    required = quota.KeepAwake.ES_CONTINUOUS | quota.KeepAwake.ES_SYSTEM_REQUIRED
    continuous = quota.KeepAwake.ES_CONTINUOUS

    with quota.KeepAwake(True):
        assert calls == [required]
        with quota.KeepAwake(True):
            assert calls == [required]
        # Inner context must not clear the outer run-level requirement.
        assert calls == [required]
    assert calls == [required, continuous]


def test_windows_batch_prompt_transport_keeps_prompt_off_argv_and_on_stdin(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-b", "main"], repo)
    run(["git", "config", "user.name", "Test"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    write(repo / ".gitignore", ".harness/\n")
    write(repo / "seed.txt", "seed\n")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "initial"], repo)

    fake = make_python_script(
        tmp_path / "fake-agent.py",
        """import json, os, sys
with open(os.environ['ARG_LOG'], 'w', encoding='utf-8') as fh:
    json.dump({'argv': sys.argv[1:], 'stdin': sys.stdin.read()}, fh)
print('OK')
""",
    )
    config_path = repo / "harness.yaml"
    write(
        config_path,
        f"""
execution:
  stall_timeout_minutes: 0
  watchdog_poll_seconds: 0.01
worktree: {{}}
cursor:
  command: {yaml_quote(fake)}
  output_format: text
  trust_harness_worktrees: true
  models: {{worker: cheap}}
verification: {{}}
paths: {{}}
context: {{}}
doctor: {{}}
""",
    )
    config = HarnessConfig.load(config_path)
    manager = WorktreeManager(repo, repo / ".harness")
    branch, worktree = manager.create("T1", "HEAD")
    runner = CursorAgentRunner(
        config,
        repo / ".harness/logs",
        repo_root=repo,
        worktree_root=manager.worktree_root,
    )

    import harness.runner as runner_module
    monkeypatch.setattr(runner_module, "cursor_prompt_via_stdin", lambda command: True)

    env = os.environ.copy()
    arg_log = tmp_path / "args.json"
    env["ARG_LOG"] = str(arg_log)
    prompt = 'multi-line prompt with "quotes" & shell chars\nsecond line ! %PATH%'
    result = runner._invoke(
        task_id="T1",
        phase="implement",
        prompt=prompt,
        workspace=worktree,
        model_class="worker",
        timeout_minutes=1,
        log_name="stdin.log",
        env=env,
    )
    assert result.ok
    payload = json.loads(arg_log.read_text(encoding="utf-8"))
    assert prompt not in payload["argv"]
    assert payload["stdin"] == prompt
    assert "--trust" in payload["argv"]
    assert "--force" in payload["argv"]
    manager.remove(worktree, branch)
