from __future__ import annotations

import importlib.util
import os
import subprocess
import time
from pathlib import Path

import pytest

from harness.code_map import (
    CodeMapBuilder,
    FALLBACK_RULES,
    graphify_rules_text,
    reset_graphify_availability_cache,
)
from harness.config import CodeMapConfig, HarnessConfig
from harness.doctor import Doctor
from harness.git_worktree import WorktreeManager
from harness.models import TaskSpec
from harness.prompts import implement_prompt, review_prompt
from harness.roadmap import Roadmap
from harness.agent_runner import AgentResult
from harness.runner import CursorAgentRunner
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier
from harness.process_utils import split_command
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


def fake_graphify(root: Path, *, fail: bool = False) -> tuple[Path, Path, Path]:
    counter = root / ".harness" / "graphify-calls.txt"
    incremental = root / ".harness" / "graphify-incremental.txt"
    body = """import pathlib
import sys

counter = pathlib.Path(sys.argv[1])
incremental = pathlib.Path(sys.argv[2])
if '--help' in sys.argv:
    print('fake graphify help')
    raise SystemExit(0)
if 'update' not in sys.argv:
    raise SystemExit(0)
counter.parent.mkdir(parents=True, exist_ok=True)
count = int(counter.read_text(encoding='utf-8')) if counter.exists() else 0
counter.write_text(str(count + 1), encoding='utf-8')
"""
    if fail:
        body += "raise SystemExit(7)\n"
    else:
        body += """
out = pathlib.Path.cwd() / 'graphify-out'
had = out.is_dir()
incremental.parent.mkdir(parents=True, exist_ok=True)
with incremental.open('a', encoding='utf-8') as handle:
    handle.write('1' if had else '0')
out.mkdir(exist_ok=True)
(out / 'graph.json').write_text((pathlib.Path.cwd() / 'source.py').read_text(encoding='utf-8'), encoding='utf-8')
(out / 'manifest.json').write_text('{}', encoding='utf-8')
(out / 'cache').mkdir(exist_ok=True)
"""
    return (
        make_python_script(root / "fake-graphify.py", body),
        counter,
        incremental,
    )


def command_for(script: Path, counter: Path, incremental: Path) -> str:
    return f'"{script}" "{counter}" "{incremental}"'


def task(task_id: str = "A") -> TaskSpec:
    return TaskSpec(id=task_id, file=Path(f"tasks/{task_id}.md"), sprint="s1", stage=1)


def builder_for(root: Path, command: str) -> CodeMapBuilder:
    return CodeMapBuilder(
        CodeMapConfig(command=command),
        root / ".harness",
        root / ".harness/logs",
        root,
    )


def expected_prompt_block(command: str) -> str:
    parts = split_command(command)
    cmd = parts[0] if parts else "graphify"
    return (
        "A code map of this repository is available in graphify-out/. You decide whether it helps. "
        "If you use it, first read graphify-out/AGENTS-graphify.md. Add `--budget 500` to "
        f"`{cmd} query`."
    )


def test_task_gets_prompt_and_worktree_copy_without_switches(tmp_path: Path):
    base_sha = init_repo(tmp_path)
    script, counter, incremental = fake_graphify(tmp_path)
    run(["git", "add", "fake-graphify.py"], tmp_path)
    run(["git", "commit", "-m", "add fake graphify"], tmp_path)
    base_sha = run(["git", "rev-parse", "HEAD"], tmp_path)
    reset_graphify_availability_cache()
    cmd = command_for(script, counter, incremental)
    builder = builder_for(tmp_path, cmd)
    branch, worktree = WorktreeManager(tmp_path, tmp_path / ".harness").create("A", base_sha)

    assert builder.ensure_project_map(base_sha)
    assert builder.offer_to_worktree(task(), worktree)

    block = expected_prompt_block(cmd)
    assert block in implement_prompt(task(), code_map_offered=True, code_map_command=cmd)
    assert block in review_prompt(task(), base_sha, "ok", code_map_offered=True, code_map_command=cmd)
    assert (worktree / "graphify-out/graph.json").is_file()
    assert (worktree / "graphify-out/AGENTS-graphify.md").is_file()
    assert (worktree / "graphify-out/manifest.json").is_file()
    assert (worktree / "graphify-out/cache").is_dir()
    assert counter.read_text(encoding="utf-8") == "1"


def test_stage_update_once_and_incremental_on_new_base(tmp_path: Path):
    base_sha = init_repo(tmp_path)
    script, counter, incremental = fake_graphify(tmp_path)
    cmd = command_for(script, counter, incremental)
    builder = builder_for(tmp_path, cmd)

    assert builder.ensure_project_map(base_sha)
    assert builder.ensure_project_map(base_sha)
    assert counter.read_text(encoding="utf-8") == "1"
    assert incremental.read_text(encoding="utf-8") == "0"

    write(tmp_path / "source.py", "VALUE = 2\n")
    run(["git", "commit", "-am", "second base"], tmp_path)
    second_sha = run(["git", "rev-parse", "HEAD"], tmp_path)

    assert builder.ensure_project_map(second_sha)
    assert counter.read_text(encoding="utf-8") == "2"
    assert incremental.read_text(encoding="utf-8") == "01"


def test_two_tasks_same_stage_share_one_project_update(tmp_path: Path):
    base_sha = init_repo(tmp_path)
    script, counter, incremental = fake_graphify(tmp_path)
    cmd = command_for(script, counter, incremental)
    builder = builder_for(tmp_path, cmd)
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")

    assert builder.ensure_project_map(base_sha)
    _, wt_a = worktrees.create("A", base_sha)
    _, wt_b = worktrees.create("B", base_sha)
    assert builder.offer_to_worktree(task("A"), wt_a)
    assert builder.offer_to_worktree(task("B"), wt_b)
    assert counter.read_text(encoding="utf-8") == "1"


def test_graphify_out_is_excluded_from_git_status_and_commit(tmp_path: Path):
    base_sha = init_repo(tmp_path)
    script, counter, incremental = fake_graphify(tmp_path)
    cmd = command_for(script, counter, incremental)
    builder = builder_for(tmp_path, cmd)
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    assert builder.ensure_project_map(base_sha)
    _, worktree = worktrees.create("A", base_sha)
    assert builder.offer_to_worktree(task(), worktree)
    write(worktree / "tracked.txt", "ok\n")
    run(["git", "add", "tracked.txt"], worktree)

    assert "graphify-out" not in run(["git", "status", "--porcelain"], worktree)
    worktrees.commit_all(worktree, "A")
    assert "graphify-out" not in run(["git", "ls-tree", "-r", "--name-only", "HEAD"], worktree)


def test_missing_or_failing_graphify_is_nonfatal(tmp_path: Path):
    base_sha = init_repo(tmp_path)
    reset_graphify_availability_cache()
    missing = builder_for(tmp_path, "definitely-missing-graphify-command")
    _, worktree = WorktreeManager(tmp_path, tmp_path / ".harness").create("A", base_sha)
    assert not missing.ensure_project_map(base_sha)
    assert not missing.offer_to_worktree(task(), worktree)
    assert "graphify-out" not in implement_prompt(task(), code_map_offered=False)

    script, counter, incremental = fake_graphify(tmp_path, fail=True)
    reset_graphify_availability_cache()
    failing = builder_for(tmp_path, command_for(script, counter, incremental))
    assert not failing.ensure_project_map(base_sha)
    assert "WARNING:" in (tmp_path / ".harness/logs/code-map.log").read_text(encoding="utf-8")


def test_doctor_warns_when_graphify_missing(tmp_path: Path):
    init_repo(tmp_path)
    reset_graphify_availability_cache()
    write(tmp_path / "harness.yaml", "code_map:\n  command: definitely-missing-graphify-command\n")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    check = {item.name: item for item in Doctor(tmp_path, config, worktrees).run()}["code-map/graphify"]
    assert not check.ok
    assert not check.required
    assert "pip install graphifyy" in check.detail


def test_legacy_config_and_frontmatter_keys_load(tmp_path: Path):
    write(tmp_path / "harness.yaml", """
code_map:
  enabled: true
  default_for_tasks: false
  command: graphify
""")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    assert config.code_map.command == "graphify"

    write(tmp_path / "tasks/a.md", "---\ncode_map: true\nallowed_paths: [x]\n---\n# A\n")
    write(tmp_path / "tasks/roadmap.yaml", """
project: x
sprints:
  s1:
    tasks:
      A: {file: tasks/a.md, stage: 1, depends_on: []}
""")
    loaded = Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml").task("A")
    assert loaded.allowed_paths == ["x"]


def test_rules_fallback_when_graphify_package_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(importlib.util, "find_spec", lambda _name: None)
    assert graphify_rules_text() == FALLBACK_RULES


def test_code_map_timeout_stops_process_tree(tmp_path: Path):
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
    reset_graphify_availability_cache()
    builder = CodeMapBuilder(
        CodeMapConfig(command=str(script), build_timeout_minutes=0.01),  # type: ignore[arg-type]
        tmp_path / ".harness",
        tmp_path / ".harness/logs",
        tmp_path,
    )
    assert not builder.ensure_project_map(base_sha)
    spawned_pid = int(child_pid.read_text(encoding="utf-8"))
    time.sleep(2)
    assert not _pid_alive(spawned_pid)


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


def test_scheduler_offers_map_in_implement_and_review(tmp_path: Path):
    base_sha = init_repo(tmp_path)
    script, counter, incremental = fake_graphify(tmp_path)
    cmd = command_for(script, counter, incremental)
    write(tmp_path / "harness.yaml", f"""
execution: {{default_max_attempts: 1, default_timeout_minutes: 1}}
cursor: {{enabled: true}}
code_map:
  command: {yaml_quote(cmd)}
verification: {{global_task_commands: [], stage_commands: []}}
""")
    reset_graphify_availability_cache()
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    state = StateStore(tmp_path / ".harness/state.json")
    worktrees = WorktreeManager(tmp_path, tmp_path / ".harness")
    verifier = Verifier(config, tmp_path / ".harness/logs", worktrees)
    runner = PromptRecordingRunner(config, tmp_path / ".harness/logs")
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier)
    task_spec = TaskSpec(
        id="A", file=Path("task.md"), sprint="s1", stage=1,
        review=True, allowed_paths=["out.txt"],
    )
    scheduler.code_maps.ensure_project_map(base_sha)
    outcome = scheduler._run_task(task_spec, base_sha)
    assert outcome.ok
    block = expected_prompt_block(cmd)
    assert block in runner.prompts["implement"]
    assert block in runner.prompts["review"]
    worktree = Path(state.get("A").worktree or "")
    assert (worktree / "graphify-out/graph.json").is_file()


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
