from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml
import pytest

from harness.codex_runner import CodexAgentRunner
from harness.config import HarnessConfig, UIReviewConfig
from harness.doctor import Doctor
from harness.git_worktree import WorktreeManager
from harness.models import TaskSpec
from harness.prompts import review_prompt
from harness.provider_runner import ProviderAgentRunner
from harness.runner import CursorAgentRunner
from harness.state import StateStore
from harness.ui_review import (
    WorktreeSnapshot,
    _collect_processes_for_worktree,
    _command_contains_worktree_path,
    _process_is_alive,
    _protected_process_ids,
    _stop_discovered_pid,
    cleanup_ui_review_processes,
    cursor_mcp_auto_approve_argv,
    worktree_changed_paths,
)
from harness.validation import ProjectValidator
from tests.portable import make_python_script, yaml_quote


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _task(tmp_path: Path) -> TaskSpec:
    task_file = tmp_path / "task.md"
    task_file.write_text("# task\n", encoding="utf-8")
    return TaskSpec(id="T1", file=task_file, sprint="S1", stage=1, review=True, allowed_paths=["**"])


def _base_yaml(tmp_path: Path, *, ui_review: dict | None = None, codex_command: str = "codex") -> Path:
    payload: dict = {
        "execution": {
            "stall_timeout_minutes": 0.1,
            "watchdog_poll_seconds": 0.01,
        },
        "cursor": {
            "enabled": True,
            "command": "agent",
            "models": {"worker": "composer-2.5", "reviewer": "composer-2.5"},
        },
        "providers": {"priority": ["codex", "cursor"]},
        "codex": {
            "enabled": True,
            "command": codex_command,
            "implementation_sequence": ["gpt-5.6-sol"],
            "review_model": "gpt-5.6-sol",
        },
        "retry": {"sequence": ["worker"]},
        "verification": {"global_task_commands": []},
        "paths": {"protected": [], "review_required": []},
    }
    if ui_review is not None:
        payload["ui_review"] = ui_review
    path = tmp_path / "harness.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _fake_codex_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake-codex.py"
    script.write_text(
        r'''from __future__ import annotations
import json, os, subprocess, sys, time
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.154.0")
    raise SystemExit(0)
if args == ["login", "status"]:
    print("Logged in using ChatGPT")
    raise SystemExit(0)
if not args or args[0] != "exec":
    raise SystemExit(2)

prompt = sys.stdin.read()
workspace = Path(args[args.index("--cd") + 1])
if not any(m in prompt for m in ("FAKE_UI_SLEEP", "FAKE_UI_DELETE_UNTRACKED", "FAKE_NEW_FILE", "FAKE_NOOP")):
    (workspace / "fake-codex-argv.json").write_text(json.dumps(args), encoding="utf-8")
    (workspace / "fake-codex-stdin.txt").write_text(prompt, encoding="utf-8")

if "FAKE_NEW_FILE" in prompt:
    (workspace / "_tmp_probe.py").write_text("x", encoding="utf-8")
if "FAKE_UI_EDIT" in prompt:
    (workspace / "tracked.txt").write_text("edited\n", encoding="utf-8")
if "FAKE_UI_DELETE_UNTRACKED" in prompt:
    target = workspace / "scratch-untracked.txt"
    if target.exists():
        target.unlink()
if "FAKE_UI_SLEEP" in prompt:
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3600)", str(workspace.resolve())],
        cwd=workspace,
    )
    pid_file = workspace / ".harness" / "ui-background.pid"
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(child.pid), encoding="utf-8")

print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"VERDICT: PASS"}}))
print(json.dumps({"type":"turn.completed","usage":{}}))
''',
        encoding="utf-8",
    )
    return script


def _init_git_worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-b", "main"], repo)
    run(["git", "config", "user.name", "Test"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    write(repo / "tracked.txt", "seed\n")
    write(repo / ".gitignore", ".harness/\n")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "initial"], repo)
    return repo


def test_review_prompt_unchanged_when_ui_review_disabled(tmp_path: Path):
    task = _task(tmp_path)
    baseline = review_prompt(task, "abc123", "ok")
    with_section = review_prompt(
        task,
        "abc123",
        "ok",
        ui_review=UIReviewConfig(enabled=False, start_commands=["npm run dev"], url="http://localhost:3000"),
    )
    assert baseline == with_section
    assert "UI check" not in baseline


def test_review_prompt_adds_ui_section_when_enabled(tmp_path: Path):
    task = _task(tmp_path)
    prompt = review_prompt(
        task,
        "abc123",
        "ok",
        ui_review=UIReviewConfig(
            enabled=True,
            start_commands=["npm run dev", "npm run api"],
            url="http://localhost:5173",
        ),
    )
    assert "UI check (your decision):" in prompt
    assert "npm run dev" in prompt
    assert "npm run api" in prompt
    assert "http://localhost:5173" in prompt
    assert "UI_CHECK: skipped" in prompt


def test_codex_review_uses_playwright_mcp_and_implementation_sandbox_when_enabled(tmp_path: Path):
    cli = _fake_codex_cli(tmp_path)
    config_path = _base_yaml(
        tmp_path,
        codex_command=str(cli),
        ui_review={
            "enabled": True,
            "start_commands": ["npm start"],
            "url": "http://localhost:3000",
            "playwright_mcp_package": "@playwright/mcp@0.0.84",
        },
    )
    config = HarnessConfig.load(config_path)
    runner = CodexAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "wt"
    workspace.mkdir()
    result = runner.review(
        _task(tmp_path), workspace, "HEAD", "verified", 1, model=config.codex.review_model
    )
    assert result.ok
    argv = json.loads((workspace / "fake-codex-argv.json").read_text(encoding="utf-8"))
    assert argv[argv.index("--sandbox") + 1] == "workspace-write"
    assert 'mcp_servers.playwright.command="npx"' in argv
    assert 'mcp_servers.playwright.args=["-y","@playwright/mcp@0.0.84"]' in argv
    assert "UI check (your decision):" in (workspace / "fake-codex-stdin.txt").read_text(encoding="utf-8")


def test_codex_review_stays_read_only_without_ui_review(tmp_path: Path):
    cli = _fake_codex_cli(tmp_path)
    config_path = _base_yaml(tmp_path, codex_command=str(cli))
    config = HarnessConfig.load(config_path)
    runner = CodexAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "wt"
    workspace.mkdir()
    runner.review(_task(tmp_path), workspace, "HEAD", "verified", 1, model=config.codex.review_model)
    argv = json.loads((workspace / "fake-codex-argv.json").read_text(encoding="utf-8"))
    assert argv[argv.index("--sandbox") + 1] == "read-only"
    assert not any("mcp_servers.playwright" in part for part in argv)


def test_provider_review_fails_when_reviewer_edits_tracked_files(tmp_path: Path):
    repo = _init_git_worktree(tmp_path)
    cli = _fake_codex_cli(tmp_path)
    config = HarnessConfig.load(
        _base_yaml(
            tmp_path,
            codex_command=str(cli),
            ui_review={"enabled": True, "start_commands": ["npm start"], "url": "http://localhost:1"},
        )
    )
    manager = WorktreeManager(repo, repo / ".harness")
    _, worktree = manager.create("T1", "HEAD")
    state = StateStore(repo / ".harness/state.json")
    codex = CodexAgentRunner(config, repo / ".harness/logs")
    cursor = CursorAgentRunner(config, repo / ".harness/logs", repo_root=repo, worktree_root=manager.worktree_root)
    router = ProviderAgentRunner(config, state, codex, cursor)
    result = router.review(
        _task(tmp_path),
        worktree,
        "HEAD",
        "FAKE_UI_EDIT",
        1,
    )
    assert not result.ok
    assert "Reviewer changed files in the worktree" in result.output
    assert "tracked.txt" in result.output


def _router_without_ui_review(tmp_path: Path):
    repo = _init_git_worktree(tmp_path)
    config = HarnessConfig.load(_base_yaml(tmp_path, codex_command=str(_fake_codex_cli(tmp_path))))
    assert not config.ui_review.enabled
    manager = WorktreeManager(repo, repo / ".harness")
    _, worktree = manager.create("T1", "HEAD")
    state = StateStore(repo / ".harness/state.json")
    codex = CodexAgentRunner(config, repo / ".harness/logs")
    cursor = CursorAgentRunner(config, repo / ".harness/logs", repo_root=repo, worktree_root=manager.worktree_root)
    return ProviderAgentRunner(config, state, codex, cursor), worktree


def test_review_fails_on_new_file_when_ui_review_disabled(tmp_path: Path):
    router, worktree = _router_without_ui_review(tmp_path)
    result = router.review(_task(tmp_path), worktree, "HEAD", "FAKE_NEW_FILE", 1)
    assert not result.ok
    assert "Reviewer changed files in the worktree" in result.output
    assert "_tmp_probe.py" in result.output


def test_review_passes_when_nothing_changes_and_ui_review_disabled(tmp_path: Path):
    router, worktree = _router_without_ui_review(tmp_path)
    result = router.review(_task(tmp_path), worktree, "HEAD", "FAKE_NOOP", 1)
    assert result.ok


def test_cleanup_receives_agent_pid_and_start_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _init_git_worktree(tmp_path)
    config = HarnessConfig.load(
        _base_yaml(
            tmp_path,
            codex_command=str(_fake_codex_cli(tmp_path)),
            ui_review={"enabled": True, "start_commands": ["npm start"], "url": "http://localhost:1"},
        )
    )
    manager = WorktreeManager(repo, repo / ".harness")
    _, worktree = manager.create("T1", "HEAD")
    state = StateStore(repo / ".harness/state.json")
    codex = CodexAgentRunner(config, repo / ".harness/logs")
    cursor = CursorAgentRunner(config, repo / ".harness/logs", repo_root=repo, worktree_root=manager.worktree_root)
    router = ProviderAgentRunner(config, state, codex, cursor)
    calls: list[dict] = []
    monkeypatch.setattr("harness.provider_runner.cleanup_ui_review_processes", lambda **kw: calls.append(kw))
    outer: list[int] = []
    router.review(_task(tmp_path), worktree, "HEAD", "FAKE_NOOP", 1, on_process_start=outer.append)
    assert len(calls) == 1 and outer
    assert calls[0]["agent_pid"] == outer[0]
    assert isinstance(calls[0]["agent_started_at"], float)


def test_worktree_changed_paths_lists_deleted_untracked_file():
    before = WorktreeSnapshot(status="?? scratch-untracked.txt\n", diff_hash="same")
    after = WorktreeSnapshot(status="", diff_hash="same")
    paths = worktree_changed_paths(before, after)
    assert paths == ["scratch-untracked.txt"]


def test_provider_review_fails_when_reviewer_deletes_untracked_file(tmp_path: Path):
    repo = _init_git_worktree(tmp_path)
    cli = _fake_codex_cli(tmp_path)
    config = HarnessConfig.load(
        _base_yaml(
            tmp_path,
            codex_command=str(cli),
            ui_review={"enabled": True, "start_commands": ["npm start"], "url": "http://localhost:1"},
        )
    )
    manager = WorktreeManager(repo, repo / ".harness")
    _, worktree = manager.create("T1", "HEAD")
    (worktree / "scratch-untracked.txt").write_text("tmp\n", encoding="utf-8")
    state = StateStore(repo / ".harness/state.json")
    codex = CodexAgentRunner(config, repo / ".harness/logs")
    cursor = CursorAgentRunner(config, repo / ".harness/logs", repo_root=repo, worktree_root=manager.worktree_root)
    router = ProviderAgentRunner(config, state, codex, cursor)
    result = router.review(
        _task(tmp_path),
        worktree,
        "HEAD",
        "FAKE_UI_DELETE_UNTRACKED",
        1,
    )
    assert not result.ok
    assert "Reviewer changed files in the worktree" in result.output
    assert "scratch-untracked.txt" in result.output


def test_provider_review_cleanup_stops_worktree_background_process(tmp_path: Path):
    repo = _init_git_worktree(tmp_path)
    cli = _fake_codex_cli(tmp_path)
    config = HarnessConfig.load(
        _base_yaml(
            tmp_path,
            codex_command=str(cli),
            ui_review={"enabled": True, "start_commands": ["npm start"], "url": "http://localhost:1"},
        )
    )
    manager = WorktreeManager(repo, repo / ".harness")
    _, worktree = manager.create("T1", "HEAD")
    state = StateStore(repo / ".harness/state.json")
    codex = CodexAgentRunner(config, repo / ".harness/logs")
    cursor = CursorAgentRunner(config, repo / ".harness/logs", repo_root=repo, worktree_root=manager.worktree_root)
    router = ProviderAgentRunner(config, state, codex, cursor)
    pid_file = worktree / ".harness/ui-background.pid"
    background_pid: int | None = None
    try:
        result = router.review(_task(tmp_path), worktree, "HEAD", "FAKE_UI_SLEEP", 1)
        assert result.ok
        background_pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and _process_is_alive(background_pid):
            time.sleep(0.1)
        assert not _process_is_alive(background_pid)
        cleanup_log = repo / ".harness/logs/T1-ui-review-cleanup.log"
        assert cleanup_log.is_file()
        assert f"stopped pid={background_pid}" in cleanup_log.read_text(encoding="utf-8")
    finally:
        if background_pid is None and pid_file.is_file():
            background_pid = int(pid_file.read_text(encoding="utf-8"))
        if background_pid is not None:
            _stop_discovered_pid(background_pid, protected=set())


@pytest.mark.parametrize("failure", ["TimeoutExpired", "OSError", "exit code 1"])
@pytest.mark.parametrize("retry_succeeds", [False, True])
def test_windows_worktree_lookup_timeout_is_logged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str, retry_succeeds: bool,
):
    worktree = tmp_path / "worktrees" / "T1"
    worktree.mkdir(parents=True)
    log_dir = tmp_path / "logs"
    calls = 0

    def fake_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        assert _kwargs["timeout"] == 90
        if retry_succeeds and calls == 2:
            return subprocess.CompletedProcess([], 0, "")
        if failure == "TimeoutExpired":
            raise subprocess.TimeoutExpired("powershell.exe", 90)
        if failure == "OSError":
            raise OSError("fake lookup launch failure")
        return subprocess.CompletedProcess([], 1, "")

    monkeypatch.setattr("harness.ui_review.os.name", "nt")
    monkeypatch.setattr("harness.ui_review._protected_process_ids", lambda: {os.getpid()})
    monkeypatch.setattr("harness.ui_review._collect_processes_for_ports", lambda *_args: {})
    monkeypatch.setattr("harness.ui_review.subprocess.run", fake_run)
    config = UIReviewConfig(enabled=True, start_commands=["x"], url="http://localhost:1", ports=[])

    cleanup_ui_review_processes(
        config=config,
        worktree=worktree,
        log_dir=log_dir,
        task_id="T1",
    )

    assert calls == 2
    expected = (
        f"worktree process lookup failed: {failure}; retry succeeded\n"
        if retry_succeeds else f"worktree process lookup failed after 2 attempts: {failure}\n"
    )
    assert (log_dir / "T1-ui-review-cleanup.log").read_text(encoding="utf-8") == expected


def test_validator_requires_start_commands_and_url_when_ui_review_enabled(tmp_path: Path):
    write(tmp_path / "harness.yaml", """
execution: {}
worktree: {}
cursor:
  models: {worker: null}
verification: {}
paths: {}
context: {}
doctor: {}
ui_review:
  enabled: true
""")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    write(tmp_path / "tasks/A.md", """---
id: A
model_class: worker
allowed_paths: [src/a.py]
verification: [python -c \"print('ok')\"]
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
    roadmap = __import__("harness.roadmap", fromlist=["Roadmap"]).Roadmap(tmp_path, tmp_path / "tasks/roadmap.yaml")
    report = ProjectValidator(tmp_path, roadmap, config).validate()
    assert not report.ok
    assert any("start_commands" in err for err in report.errors)
    assert any("url" in err for err in report.errors)


def test_doctor_ui_review_checks_use_fakes(tmp_path: Path, monkeypatch):
    run(["git", "init", "-b", "main"], tmp_path)
    write(tmp_path / "harness.yaml", """
execution: {}
worktree: {}
cursor:
  enabled: true
  command: agent
  models: {worker: null}
verification: {}
paths: {}
context: {}
doctor: {}
ui_review:
  enabled: true
  start_commands: [npm start]
  url: http://localhost:3000
  ports: [3000]
""")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    monkeypatch.setattr("harness.doctor.locate_executable", lambda name: None if name == "npx" else "/bin/npx")
    monkeypatch.setattr("harness.doctor.port_in_use", lambda port: port == 3000)
    monkeypatch.setattr("harness.doctor.cursor_mcp_config_mentions_playwright", lambda path: False)
    checks = Doctor(tmp_path, config, WorktreeManager(tmp_path, tmp_path / ".harness")).run(full=False)
    by_name = {check.name: check for check in checks}
    assert by_name["ui-review/npx"].ok is False
    assert by_name["ui-review/port:3000"].ok is False
    assert by_name["ui-review/cursor-playwright-mcp"].ok is False
    assert by_name["ui-review/port:3000"].required is False
    assert by_name["ui-review/cursor-playwright-mcp"].required is False


def _fake_cursor_agent(tmp_path: Path, *, help_text: str, name: str = "fake-agent.py") -> Path:
    return make_python_script(
        tmp_path / name,
        f"""import json, os, sys
if "--help" in sys.argv:
    print({help_text!r})
    raise SystemExit(0)
log = os.environ.get("ARG_LOG")
if log:
    with open(log, "w", encoding="utf-8") as fh:
        json.dump(sys.argv[1:], fh)
print("VERDICT: PASS")
""",
    )


def _cursor_review_args(tmp_path: Path, fake: Path) -> list[str]:
    config_path = tmp_path / "harness.yaml"
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
  models: {{reviewer: cheap}}
verification: {{}}
paths: {{}}
context: {{}}
doctor: {{}}
ui_review:
  enabled: true
  start_commands: [npm start]
  url: http://localhost:1
""",
    )
    config = HarnessConfig.load(config_path)
    runner = CursorAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "wt"
    workspace.mkdir()
    env = os.environ.copy()
    arg_log = tmp_path / "review-args.json"
    env["ARG_LOG"] = str(arg_log)
    result = runner.review(_task(tmp_path), workspace, "HEAD", "ok", 1, env=env)
    assert result.ok
    return json.loads(arg_log.read_text(encoding="utf-8"))


def test_cursor_mcp_auto_approve_argv_parses_help(tmp_path: Path):
    with_flag = _fake_cursor_agent(
        tmp_path,
        help_text="Usage: agent [options]\\n  --approve-mcps    Approve MCP servers\\n",
        name="agent-with-flag.py",
    )
    without_flag = _fake_cursor_agent(
        tmp_path,
        help_text="Usage: agent [options]\\n  --force    Force\\n  --trust    Trust\\n",
        name="agent-without-flag.py",
    )
    assert cursor_mcp_auto_approve_argv(str(with_flag)) == ["--approve-mcps"]
    assert cursor_mcp_auto_approve_argv(str(without_flag)) == []


def test_cursor_review_adds_approve_mcps_when_enabled(tmp_path: Path):
    fake = _fake_cursor_agent(
        tmp_path,
        help_text="Usage: agent [--approve-mcps] [--trust]\\n",
    )
    args = _cursor_review_args(tmp_path, fake)
    assert "--approve-mcps" in args


def test_cursor_review_omits_approve_mcps_when_cli_lacks_flag(tmp_path: Path):
    fake = _fake_cursor_agent(
        tmp_path,
        help_text="Usage: agent [--force] [--trust]\\n",
    )
    args = _cursor_review_args(tmp_path, fake)
    assert "--approve-mcps" not in args


def test_cleanup_skips_port_listeners_present_before_review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _init_git_worktree(tmp_path)
    manager = WorktreeManager(repo, repo / ".harness")
    _, worktree = manager.create("T1", "HEAD")
    protected = {999999}
    before = {101: "pre-existing listener"}
    after = {101: "pre-existing listener", 202: "new listener"}

    def fake_collect(ports, prot):
        if ports == [3000]:
            return dict(after)
        return {}

    monkeypatch.setattr("harness.ui_review._protected_process_ids", lambda: protected)
    monkeypatch.setattr("harness.ui_review._collect_processes_for_ports", fake_collect)
    stopped: list[int] = []
    monkeypatch.setattr(
        "harness.ui_review._stop_discovered_pid",
        lambda pid, prot: stopped.append(pid),
    )
    monkeypatch.setattr("harness.ui_review._process_is_alive", lambda _pid: True)
    monkeypatch.setattr("harness.ui_review._collect_processes_for_worktree", lambda *_args: {})

    config = UIReviewConfig(enabled=True, start_commands=["x"], url="http://localhost:1", ports=[3000])
    cleanup_ui_review_processes(
        config=config,
        worktree=worktree,
        log_dir=repo / ".harness/logs",
        task_id="T1",
        ports_before=before,
    )
    assert stopped == [202]


def test_cleanup_ui_review_processes_direct_call(tmp_path: Path):
    repo = _init_git_worktree(tmp_path)
    manager = WorktreeManager(repo, repo / ".harness")
    _, worktree = manager.create("T1", "HEAD")
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(3600)", str(worktree.resolve())],
        cwd=worktree,
    )
    config = UIReviewConfig(enabled=True, start_commands=["x"], url="http://localhost:1", ports=[])
    cleanup_ui_review_processes(
        config=config,
        worktree=worktree,
        log_dir=repo / ".harness/logs",
        task_id="T1",
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    else:
        proc.kill()
        raise AssertionError("cleanup_ui_review_processes did not stop background process")


def _ps_format_blob(argv: list[str]) -> str:
    blobs: list[str] = []
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg in {"-o", "-eo", "-oe"}:
            assert index + 1 < len(argv), f"{arg} missing format argument in {argv}"
            blobs.append(argv[index + 1])
            index += 2
            continue
        if arg in {"-e", "-a", "-x", "-ax", "-A", "-w", "-ww"}:
            index += 1
            continue
        raise AssertionError(f"dangling ps operand {arg!r} in {argv}")
    return ",".join(blobs)


def test_posix_worktree_discovery_uses_ps_format_argument(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "wt"
    worktree.mkdir()
    needle = str(worktree.resolve())
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        recorded = [str(part) for part in argv]
        calls.append(recorded)

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        if recorded and recorded[0] == "ps":
            blob = _ps_format_blob(recorded)
            assert "pid" in blob
            if "args" in blob or "command" in blob:
                Result.stdout = f" 4242 python app.py {needle}\n"
            elif "ppid" in blob:
                Result.stdout = " 4242  1\n"
        return Result()

    monkeypatch.setattr("harness.ui_review.os.name", "posix")
    monkeypatch.setattr("harness.ui_review.subprocess.run", fake_run)
    targets = _collect_processes_for_worktree(worktree, protected=set())
    assert calls, "POSIX worktree discovery must invoke ps"
    assert 4242 in targets
    assert needle in targets[4242]


def test_stop_discovered_pid_does_not_signal_process_group(monkeypatch):
    killed: list[int] = []

    def fake_kill(pid, sig):
        killed.append(pid)

    def boom(*_args, **_kwargs):
        raise AssertionError("killpg must not be used for leftover review processes")

    monkeypatch.setattr("harness.ui_review.os.name", "posix")
    monkeypatch.setattr("harness.ui_review.os.kill", fake_kill)
    monkeypatch.setattr("harness.ui_review.os.killpg", boom, raising=False)
    monkeypatch.setattr("harness.ui_review._posix_descendant_pids", lambda pid: [pid + 1])
    monkeypatch.setattr("harness.ui_review._process_is_alive", lambda pid: False)
    _stop_discovered_pid(4242, protected=set())
    assert killed == [4243, 4242]


def _sibling_worktrees(tmp_path: Path) -> tuple[Path, Path]:
    short = tmp_path / "worktrees" / "S1-T1"
    long = tmp_path / "worktrees" / "S1-T10"
    short.mkdir(parents=True)
    long.mkdir(parents=True)
    return short, long


def test_command_contains_worktree_path_rejects_sibling_prefix_ids(tmp_path: Path):
    short, long = _sibling_worktrees(tmp_path)
    short_s = str(short.resolve())
    long_s = str(long.resolve())
    assert _command_contains_worktree_path(f"python app.py {short_s}", short)
    assert _command_contains_worktree_path(f'python app.py "{short_s}"', short)
    assert _command_contains_worktree_path(f"python app.py {short_s}/src/main.py", short)
    assert not _command_contains_worktree_path(f"python app.py {long_s}", short)
    assert _command_contains_worktree_path(f"python app.py {long_s}", long)
    assert not _command_contains_worktree_path("python app.py /tmp/unrelated", short)


def test_posix_worktree_discovery_ignores_sibling_prefix_ids(monkeypatch, tmp_path: Path):
    short, long = _sibling_worktrees(tmp_path)
    short_s = str(short.resolve())
    long_s = str(long.resolve())

    def fake_run(argv, **kwargs):
        recorded = [str(part) for part in argv]

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        if recorded and recorded[0] == "ps":
            blob = _ps_format_blob(recorded)
            if "args" in blob or "command" in blob:
                Result.stdout = (
                    f" 111 python --cd {short_s} app.py\n"
                    f" 222 python --cd {long_s} app.py\n"
                )
        return Result()

    monkeypatch.setattr("harness.ui_review.os.name", "posix")
    monkeypatch.setattr("harness.ui_review.subprocess.run", fake_run)
    targets = _collect_processes_for_worktree(short, protected=set())
    assert 111 in targets
    assert 222 not in targets
    long_targets = _collect_processes_for_worktree(long, protected=set())
    assert 222 in long_targets
    assert 111 not in long_targets


def test_windows_worktree_discovery_ignores_sibling_prefix_ids(monkeypatch, tmp_path: Path):
    short, long = _sibling_worktrees(tmp_path)
    short_s = str(short.resolve())
    long_s = str(long.resolve())

    def fake_run(argv, **kwargs):
        class Result:
            returncode = 0
            stdout = (
                f"111\t1\t2026-11-03T00:00:01+00:00\tpython --cd {short_s} app.py\n"
                f"222\t1\t2026-11-03T00:00:01+00:00\tpython --cd {long_s} app.py\n"
            )
            stderr = ""

        return Result()

    monkeypatch.setattr("harness.ui_review.os.name", "nt")
    monkeypatch.setattr("harness.ui_review.subprocess.run", fake_run)
    targets = _collect_processes_for_worktree(short, protected=set())
    assert 111 in targets
    assert 222 not in targets
    long_targets = _collect_processes_for_worktree(long, protected=set())
    assert 222 in long_targets
    assert 111 not in long_targets


@pytest.mark.parametrize(
    ("agent_pid", "started_at", "expected"),
    [(100, 1000.0, [101, 102]), (None, None, []), (100, None, []), (999, 1000.0, [])],
)
def test_windows_cleanup_includes_exited_agent_descendants(
    monkeypatch, tmp_path: Path, agent_pid, started_at, expected,
):
    worktree = tmp_path / "worktrees" / "S1-T1"
    worktree.mkdir(parents=True)
    # Agent 100 is absent; 999 is the harness and 200 belongs to another task.
    rows = [
        (101, 100, "1970-01-01T00:16:41+00:00"),
        (102, 101, "1970-01-01T00:16:42+00:00"),
        (201, 200, "1970-01-01T00:16:41+00:00"),
        (202, 999, "1970-01-01T00:16:41+00:00"),
        (301, 100, "1970-01-01T00:16:39+00:00"),  # stale parent PID
        (302, 301, "1970-01-01T00:16:42+00:00"),  # stale intermediate
        (401, 100, ""),  # unknown creation time
        (501, 502, "1970-01-01T00:16:41+00:00"),  # cycle
        (502, 501, "1970-01-01T00:16:41+00:00"),
        (601, 100, "1970-01-01T00:16:40+00:00"),  # not after start
    ]

    def fake_run(argv, **kwargs):
        assert "CreationDate" in argv[-1]
        assert kwargs["timeout"] == 90
        return subprocess.CompletedProcess(
            argv, 0,
            "".join(f"{pid}\t{parent}\t{created}\tpython -c sleep\n"
                    for pid, parent, created in rows),
        )

    stopped: list[int] = []
    monkeypatch.setattr("harness.ui_review.os.name", "nt")
    monkeypatch.setattr("harness.ui_review.os.getpid", lambda: 999)
    monkeypatch.setattr("harness.ui_review._protected_process_ids", lambda: set())
    monkeypatch.setattr("harness.ui_review._collect_processes_for_ports", lambda *_args: {})
    monkeypatch.setattr("harness.ui_review.subprocess.run", fake_run)
    monkeypatch.setattr("harness.ui_review._process_is_alive", lambda _pid: True)
    monkeypatch.setattr("harness.ui_review._stop_discovered_pid", lambda pid, _: stopped.append(pid))
    cleanup_ui_review_processes(
        config=UIReviewConfig(enabled=True),
        worktree=worktree,
        log_dir=tmp_path / "logs",
        task_id="T1",
        agent_pid=agent_pid,
        agent_started_at=started_at,
    )
    assert stopped == expected


def test_posix_worktree_discovery_skips_protected_parent_pid(monkeypatch, tmp_path: Path):
    worktree = tmp_path / "worktrees" / "S1-T1"
    worktree.mkdir(parents=True)
    needle = str(worktree.resolve())

    def fake_run(argv, **kwargs):
        recorded = [str(part) for part in argv]

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        if recorded and recorded[0] == "ps":
            blob = _ps_format_blob(recorded)
            if "args" in blob or "command" in blob:
                Result.stdout = (
                    f"  50 harness --cd {needle}\n"
                    f"4242 python -c sleep {needle}\n"
                )
        return Result()

    monkeypatch.setattr("harness.ui_review.os.name", "posix")
    monkeypatch.setattr("harness.ui_review.subprocess.run", fake_run)
    targets = _collect_processes_for_worktree(worktree, protected={50})
    assert 50 not in targets
    assert 4242 in targets


def test_posix_protected_process_ids_walks_parents(monkeypatch):
    ppid_by_pid = {100: 50, 50: 7, 7: 1, 1: 0}
    queried: list[int] = []

    def fake_run(argv, **kwargs):
        recorded = [str(part) for part in argv]

        class Result:
            returncode = 0
            stdout = ""
            stderr = ""

        assert recorded[:1] == ["ps"], recorded
        assert "-p" in recorded, recorded
        pid = int(recorded[recorded.index("-p") + 1])
        queried.append(pid)
        Result.stdout = f" {ppid_by_pid[pid]}\n"
        return Result()

    monkeypatch.setattr("harness.ui_review.os.name", "posix")
    monkeypatch.setattr("harness.ui_review.os.getpid", lambda: 100)
    monkeypatch.setattr("harness.ui_review.subprocess.run", fake_run)
    protected = _protected_process_ids()
    assert protected == {100, 50, 7, 1}
    assert queried == [100, 50, 7, 1]
    assert 0 not in protected
