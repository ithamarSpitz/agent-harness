from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.code_map import CodeMapBuilder
from harness.cli import cmd_retry, cmd_review, cmd_unblock
from harness.agent_runner import AgentResult
from harness.config import CodeMapConfig, HarnessConfig
from harness.doctor import Doctor
from harness.git_worktree import WorktreeManager
from harness.models import TaskSpec, TaskStatus
from harness.prompts import implement_prompt, review_prompt
from harness.runner import CursorAgentRunner
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier
from tests.portable import make_python_script, yaml_quote


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def init_repo(root: Path) -> str:
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    write(root / ".gitignore", ".harness/\n")
    write(root / "source.py", "VALUE = 1\n")
    run(["git", "add", "."], root)
    run(["git", "commit", "-m", "initial"], root)
    return run(["git", "rev-parse", "HEAD"], root)


def fake_graphify(root: Path, *, fail: bool = False) -> tuple[Path, Path]:
    counter = root / ".harness" / "graphify-calls.txt"
    body = """import pathlib
import sys

counter = pathlib.Path(sys.argv[1])
counter.parent.mkdir(parents=True, exist_ok=True)
count = int(counter.read_text(encoding='utf-8')) if counter.exists() else 0
counter.write_text(str(count + 1), encoding='utf-8')
if '--help' in sys.argv:
    print('fake graphify help')
    raise SystemExit(0)
"""
    if fail:
        body += "raise SystemExit(7)\n"
    else:
        body += """out = pathlib.Path.cwd() / 'graphify-out'
out.mkdir(exist_ok=True)
(out / 'graph.json').write_text((pathlib.Path.cwd() / 'source.py').read_text(encoding='utf-8'), encoding='utf-8')
"""
    return make_python_script(root / "fake-graphify.py", body), counter


def command_for(script: Path, counter: Path) -> str:
    return f'"{script}" "{counter}"'


def task(task_id: str = "A", setting: bool | None = True) -> TaskSpec:
    return TaskSpec(
        id=task_id, file=Path(f"tasks/{task_id}.md"), sprint="s1", stage=1,
        code_map=setting,
    )


def test_code_map_cache_is_keyed_by_base_commit_and_leaves_worktree_clean(tmp_path: Path):
    init_repo(tmp_path)
    script, counter = fake_graphify(tmp_path)
    run(["git", "add", "fake-graphify.py"], tmp_path)
    run(["git", "commit", "-m", "add fake graphify"], tmp_path)
    first_sha = run(["git", "rev-parse", "HEAD"], tmp_path)
    config = CodeMapConfig(enabled=True, command=command_for(script, counter))
    builder = CodeMapBuilder(config, tmp_path / ".harness", tmp_path / ".harness/logs")

    first = builder.prepare(task("A"), tmp_path, first_sha)
    reused = builder.prepare(task("B"), tmp_path, first_sha)

    assert first == reused
    assert first == (tmp_path / ".harness/code-map" / first_sha / "graph.json").resolve()
    assert first.is_file()
    assert first.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert counter.read_text(encoding="utf-8") == "1"
    assert not (tmp_path / "graphify-out").exists()
    assert run(["git", "status", "--porcelain"], tmp_path) == ""

    write(tmp_path / "source.py", "VALUE = 2\n")
    run(["git", "commit", "-am", "new base"], tmp_path)
    second_sha = run(["git", "rev-parse", "HEAD"], tmp_path)
    second = builder.prepare(task("C"), tmp_path, second_sha)

    assert second != first
    assert second == (tmp_path / ".harness/code-map" / second_sha / "graph.json").resolve()
    assert second.read_text(encoding="utf-8") == "VALUE = 2\n"
    assert counter.read_text(encoding="utf-8") == "2"
    assert not (tmp_path / "graphify-out").exists()


@pytest.mark.parametrize("cached", [False, True])
def test_cache_only_prepare_preserves_project_output(tmp_path: Path, cached: bool):
    base_sha = init_repo(tmp_path)
    script, counter = fake_graphify(tmp_path)
    builder = CodeMapBuilder(
        CodeMapConfig(enabled=True, command=command_for(script, counter)),
        tmp_path / ".harness", tmp_path / ".harness/logs",
    )
    expected = builder.prepare(task(), tmp_path, base_sha) if cached else None
    project_output = tmp_path / "graphify-out/project.txt"
    write(project_output, "project-owned output")

    assert builder.prepare(task(), tmp_path, base_sha, allow_build=False) == expected
    if cached:
        assert builder.prepare(task(), tmp_path, base_sha) == expected
    assert project_output.read_text(encoding="utf-8") == "project-owned output"
    assert (counter.read_text(encoding="utf-8") if counter.exists() else "0") == (
        "1" if cached else "0"
    )


def test_code_map_failures_are_nonfatal_and_disabled_tasks_do_not_run(tmp_path: Path):
    base_sha = init_repo(tmp_path)
    script, counter = fake_graphify(tmp_path, fail=True)
    enabled = CodeMapBuilder(
        CodeMapConfig(enabled=True, command=command_for(script, counter)),
        tmp_path / ".harness", tmp_path / ".harness/logs",
    )

    assert enabled.prepare(task(), tmp_path, base_sha) is None
    assert "WARNING: code map build failed" in (
        tmp_path / ".harness/logs/A-code-map.log"
    ).read_text(encoding="utf-8")
    assert not (tmp_path / "graphify-out").exists()
    assert enabled.prepare(task("B", False), tmp_path, base_sha) is None
    assert counter.read_text(encoding="utf-8") == "1"

    disabled = CodeMapBuilder(
        CodeMapConfig(enabled=False, command=command_for(script, counter)),
        tmp_path / ".harness", tmp_path / ".harness/logs",
    )
    assert disabled.prepare(task("C"), tmp_path, base_sha) is None
    assert counter.read_text(encoding="utf-8") == "1"

    defaulted = CodeMapBuilder(
        CodeMapConfig(
            enabled=True, command=command_for(script, counter), default_for_tasks=False
        ),
        tmp_path / ".harness", tmp_path / ".harness/logs",
    )
    assert defaulted.prepare(task("C2", None), tmp_path, base_sha) is None
    assert counter.read_text(encoding="utf-8") == "1"

    missing = CodeMapBuilder(
        CodeMapConfig(enabled=True, command="definitely-missing-graphify-command"),
        tmp_path / ".harness", tmp_path / ".harness/logs",
    )
    assert missing.prepare(task("D"), tmp_path, base_sha) is None
    assert "pip install graphifyy" in (
        tmp_path / ".harness/logs/D-code-map.log"
    ).read_text(encoding="utf-8")


def test_code_map_timeout_stops_process_tree_before_cleanup(tmp_path: Path):
    base_sha = init_repo(tmp_path)
    child_pid = tmp_path / ".harness/graphify-child.pid"
    child_code = (
        "import pathlib, time\n"
        "time.sleep(1.5)\n"
        "out = pathlib.Path.cwd() / 'graphify-out'\n"
        "out.mkdir(exist_ok=True)\n"
        "(out / 'graph.json').write_text('late', encoding='utf-8')\n"
    )
    script = make_python_script(
        tmp_path / "slow-graphify.py",
        "import pathlib, subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        f"pathlib.Path({str(child_pid)!r}).parent.mkdir(parents=True, exist_ok=True)\n"
        f"pathlib.Path({str(child_pid)!r}).write_text(str(child.pid), encoding='utf-8')\n"
        "time.sleep(120)\n",
    )
    builder = CodeMapBuilder(
        CodeMapConfig(
            enabled=True,
            command=str(script),
            build_timeout_minutes=0.01,  # type: ignore[arg-type]
        ),
        tmp_path / ".harness",
        tmp_path / ".harness/logs",
    )

    assert builder.prepare(task(), tmp_path, base_sha) is None
    assert "timed out" in (tmp_path / ".harness/logs/A-code-map.log").read_text(
        encoding="utf-8"
    )
    spawned_pid = int(child_pid.read_text(encoding="utf-8"))
    time.sleep(2)
    assert not _pid_alive(spawned_pid)
    assert not (tmp_path / "graphify-out").exists()


def test_prompts_add_exact_code_map_guidance_only_when_available(tmp_path: Path):
    graph = (tmp_path / ".harness/code-map/abc/graph.json").resolve()
    expected = f"""Code map: {graph}
To locate code, use `custom-graphify explain "<symbol or file>" --graph "{graph}"` or `custom-graphify path "<A>" "<B>" --graph "{graph}"`. Their output is small.
Do not use `custom-graphify query` (very large output) and do not open the graph file. Use the map only when you need to find where something is defined or used."""

    implement = implement_prompt(task(), code_map_path=graph, code_map_command="custom-graphify")
    review = review_prompt(
        task(), "abc", "ok", code_map_path=graph, code_map_command="custom-graphify"
    )
    assert expected in implement
    assert expected in review
    assert "Code map:" not in implement_prompt(task())
    assert "Code map:" not in review_prompt(task(), "abc", "ok")


class PromptRecordingRunner(CursorAgentRunner):
    def __init__(self, config: HarnessConfig, log_dir: Path) -> None:
        super().__init__(config, log_dir)
        self.prompts: dict[str, str] = {}

    def _invoke(self, **kwargs) -> AgentResult:
        phase = kwargs["phase"]
        self.prompts[phase] = kwargs["prompt"]
        if phase == "implement":
            (kwargs["workspace"] / "out.txt").write_text("done\n", encoding="utf-8")
            return AgentResult(True, "implemented")
        return AgentResult(True, "VERDICT: PASS\n")


def scheduler_for(root: Path, config: HarnessConfig) -> tuple[Scheduler, StateStore, PromptRecordingRunner]:
    state = StateStore(root / ".harness/state.json")
    worktrees = WorktreeManager(root, root / ".harness")
    verifier = Verifier(config, root / ".harness/logs", worktrees)
    runner = PromptRecordingRunner(config, root / ".harness/logs")
    return Scheduler(root, config, state, worktrees, runner, verifier), state, runner


def mapped_task() -> TaskSpec:
    return TaskSpec(
        id="A", file=Path("task.md"), sprint="s1", stage=1,
        review=True, allowed_paths=["out.txt"], code_map=True,
    )


def expected_map_block(path: Path, command: str) -> str:
    return f'''Code map: {path}
To locate code, use `{command} explain "<symbol or file>" --graph "{path}"` or `{command} path "<A>" "<B>" --graph "{path}"`. Their output is small.
Do not use `{command} query` (very large output) and do not open the graph file. Use the map only when you need to find where something is defined or used.'''


def test_scheduler_supplies_same_cached_map_to_implement_and_review(tmp_path: Path):
    base_sha = init_repo(tmp_path)
    script, counter = fake_graphify(tmp_path)
    write(tmp_path / "harness.yaml", f"""
execution: {{default_max_attempts: 1, default_timeout_minutes: 1}}
code_map:
  enabled: true
  command: {yaml_quote(command_for(script, counter))}
verification: {{global_task_commands: [], stage_commands: []}}
""")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    scheduler, state, runner = scheduler_for(tmp_path, config)

    outcome = scheduler._run_task(mapped_task(), base_sha)

    expected = (tmp_path / ".harness/code-map" / base_sha / "graph.json").resolve()
    assert outcome.ok
    block = expected_map_block(expected, config.code_map.command)
    assert block in runner.prompts["implement"]
    assert block in runner.prompts["review"]
    assert counter.read_text(encoding="utf-8") == "1"
    worktree = Path(state.get("A").worktree or "")
    assert not (worktree / "graphify-out").exists()


def test_scheduler_resume_before_first_attempt_builds_map(tmp_path: Path):
    base_sha = init_repo(tmp_path)
    script, counter = fake_graphify(tmp_path)
    write(tmp_path / "harness.yaml", f"""
execution: {{default_max_attempts: 1, default_timeout_minutes: 1}}
code_map:
  enabled: true
  command: {yaml_quote(command_for(script, counter))}
verification: {{global_task_commands: [], stage_commands: []}}
""")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    scheduler, state, runner = scheduler_for(tmp_path, config)
    branch, worktree = scheduler.worktrees.create("A", base_sha)
    runtime = state.get("A")
    runtime.branch = branch
    runtime.worktree = str(worktree)
    runtime.base_ref = base_sha
    state.save()

    outcome = scheduler._run_task(mapped_task(), base_sha)

    expected = (tmp_path / ".harness/code-map" / base_sha / "graph.json").resolve()
    assert outcome.ok
    assert expected_map_block(expected, config.code_map.command) in runner.prompts["implement"]
    assert counter.read_text(encoding="utf-8") == "1"


@pytest.mark.parametrize("recovery", ["quota", "capacity", "retry", "unblock"])
def test_scheduler_never_builds_map_after_implementation_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str
):
    base_sha = init_repo(tmp_path)
    script, counter = fake_graphify(tmp_path, fail=True)
    write(tmp_path / "harness.yaml", f"""
execution: {{default_max_attempts: 1, default_timeout_minutes: 1}}
code_map:
  enabled: true
  command: {yaml_quote(command_for(script, counter))}
verification: {{global_task_commands: [], stage_commands: []}}
""")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    scheduler, state, runner = scheduler_for(tmp_path, config)

    def change_then_stop(**kwargs):
        write(kwargs["workspace"] / "source.py", "VALUE = 999\n")
        if recovery == "quota":
            return AgentResult(False, "quota exhausted", quota_exhausted=True)
        if recovery == "capacity":
            return AgentResult(False, "capacity exhausted", capacity_exhausted=True)
        return AgentResult(False, "implementation failed")

    monkeypatch.setattr(runner, "_invoke", change_then_stop)
    assert not scheduler._run_task(mapped_task(), base_sha).ok
    runtime = state.get("A")
    assert runtime.started_at is not None
    if recovery == "retry":
        assert cmd_retry(state, "A") == 0
    elif recovery == "unblock":
        assert runtime.status == TaskStatus.BLOCKED
        assert cmd_unblock(state, "A") == 0
    assert state.get("A").attempt == 0

    # Make the previously failing builder available. A resume must not build
    # from the retained worktree now that it contains implementation changes.
    fake_graphify(tmp_path)
    scheduler, _, runner = scheduler_for(tmp_path, config)

    def pause_again(**kwargs):
        return AgentResult(False, "capacity exhausted", capacity_exhausted=True)

    monkeypatch.setattr(runner, "_invoke", pause_again)
    assert not scheduler._run_task(mapped_task(), base_sha).ok
    graph = tmp_path / ".harness/code-map" / base_sha / "graph.json"
    assert counter.read_text(encoding="utf-8") == "1"
    assert not graph.exists()


@pytest.mark.parametrize("failure_kind", ["failing", "missing", "malformed", "git_timeout"])
def test_scheduler_continues_without_map_prompt_when_build_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure_kind: str
):
    base_sha = init_repo(tmp_path)
    if failure_kind == "failing":
        script, _ = fake_graphify(tmp_path, fail=True)
        command = command_for(script, tmp_path / ".harness/graphify-calls.txt")
    elif failure_kind == "missing":
        command = "definitely-missing-graphify-command"
    elif failure_kind == "git_timeout":
        script, counter = fake_graphify(tmp_path)
        command = command_for(script, counter)
        original_run = subprocess.run

        def timeout_base_lookup(args, **kwargs):
            if args == ["git", "rev-parse", f"{base_sha}^{{commit}}"]:
                raise subprocess.TimeoutExpired(args, kwargs["timeout"])
            return original_run(args, **kwargs)

        monkeypatch.setattr("harness.code_map.subprocess.run", timeout_base_lookup)
    else:
        command = '"unterminated'
    write(tmp_path / "harness.yaml", f"""
execution: {{default_max_attempts: 1, default_timeout_minutes: 1}}
code_map:
  enabled: true
  command: {yaml_quote(command)}
verification: {{global_task_commands: [], stage_commands: []}}
""")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    scheduler, _, runner = scheduler_for(tmp_path, config)

    outcome = scheduler._run_task(mapped_task(), base_sha)

    assert outcome.ok
    assert "Code map:" not in runner.prompts["implement"]
    assert "Code map:" not in runner.prompts["review"]
    warning = (tmp_path / ".harness/logs/A-code-map.log").read_text(encoding="utf-8")
    assert warning.startswith("WARNING: code map ")
    if failure_kind == "git_timeout":
        assert len(warning.splitlines()) == 1
        assert "timed out" in warning
        assert not counter.exists()


@pytest.mark.parametrize("entrypoint", ["resume", "manual_review"])
@pytest.mark.parametrize("cache_state", ["failed_build", "deleted", "present"])
def test_review_after_pause_never_builds_from_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str, cache_state: str
):
    base_sha = init_repo(tmp_path)
    script, counter = fake_graphify(tmp_path, fail=cache_state == "failed_build")
    write(tmp_path / "harness.yaml", f"""
execution: {{default_max_attempts: 1, default_timeout_minutes: 1}}
code_map:
  enabled: true
  command: {yaml_quote(command_for(script, counter))}
verification: {{global_task_commands: [], stage_commands: []}}
""")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    scheduler, state, runner = scheduler_for(tmp_path, config)
    original_invoke = runner._invoke

    def pause_review(**kwargs):
        if kwargs["phase"] == "review":
            return AgentResult(False, "capacity exhausted", capacity_exhausted=True)
        return original_invoke(**kwargs)

    monkeypatch.setattr(runner, "_invoke", pause_review)
    scheduler._run_task(mapped_task(), base_sha)
    assert state.get("A").status == TaskStatus.WAITING_FOR_CAPACITY
    worktree = Path(state.get("A").worktree or "")
    assert (worktree / "out.txt").is_file()
    graph = tmp_path / ".harness/code-map" / base_sha / "graph.json"
    if cache_state == "deleted":
        graph.unlink()
    # The command now succeeds and would capture implementation changes if called.
    fake_graphify(tmp_path)
    write(worktree / "source.py", "VALUE = 999\n")
    write(worktree / "graphify-out/project.txt", "keep me")
    resumed_task = mapped_task()
    resumed_task.allowed_paths += ["source.py", "graphify-out/**"]
    monkeypatch.setattr(runner, "_invoke", original_invoke)
    runner.prompts.clear()

    if entrypoint == "resume":
        # Reload persisted state as a new harness process would.
        scheduler, _, runner = scheduler_for(tmp_path, config)
        assert scheduler._run_task(resumed_task, base_sha).ok
    else:
        runtime = SimpleNamespace(
            roadmap=SimpleNamespace(task=lambda task_id: resumed_task),
            state=state, environment=scheduler.environment,
            scheduler=scheduler, runner=runner,
        )
        assert cmd_review(runtime, "A", 1) == 0

    assert "implement" not in runner.prompts
    assert counter.read_text(encoding="utf-8") == "1"
    assert (worktree / "graphify-out/project.txt").read_text(encoding="utf-8") == "keep me"
    if cache_state == "present":
        assert graph.read_text(encoding="utf-8") == "VALUE = 1\n"
        assert "Code map:" in runner.prompts["review"]
    else:
        assert not graph.exists()
        assert "Code map:" not in runner.prompts["review"]


def doctor_config(root: Path, *, enabled: bool, command: str = "graphify") -> HarnessConfig:
    write(root / "harness.yaml", f"""
cursor: {{enabled: false}}
codex: {{enabled: false}}
code_map:
  enabled: {str(enabled).lower()}
  command: {yaml_quote(command)}
""")
    return HarnessConfig.load(root / "harness.yaml")


def test_doctor_checks_graphify_only_when_enabled(tmp_path: Path):
    init_repo(tmp_path)
    script, counter = fake_graphify(tmp_path)
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")

    working = doctor_config(tmp_path, enabled=True, command=command_for(script, counter))
    checks = Doctor(tmp_path, working, worktrees).run()
    assert {check.name: check for check in checks}["code-map/graphify"].ok

    missing = doctor_config(tmp_path, enabled=True, command="definitely-missing-graphify-command")
    missing_check = {check.name: check for check in Doctor(tmp_path, missing, worktrees).run()}[
        "code-map/graphify"
    ]
    assert not missing_check.ok
    assert "pip install graphifyy" in missing_check.detail

    disabled = doctor_config(tmp_path, enabled=False)
    assert "code-map/graphify" not in {
        check.name for check in Doctor(tmp_path, disabled, worktrees).run()
    }


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
