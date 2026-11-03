from __future__ import annotations

import subprocess
from pathlib import Path

import yaml

from harness.codex_runner import CodexAgentRunner
from harness.config import HarnessConfig
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.models import TaskSpec, TaskStatus, WINDOWS_RESOURCE_EXIT_CODES
from harness.provider_runner import ProviderAgentRunner
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.usage import UsageRecorder
from harness.verifier import Verifier
from tests.portable import file_exists_command, yaml_quote


def test_codex_launch_failure_usage_is_not_auth(tmp_path: Path, monkeypatch) -> None:
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    runner = CodexAgentRunner(_codex_config(tmp_path, "codex"), tmp_path / "logs", usage=usage)

    def fail_launch(*args, **kwargs):
        raise FileNotFoundError("CLI not found")

    monkeypatch.setattr("harness.codex_runner.subprocess.Popen", fail_launch)
    result = runner._invoke(
        task_id="LAUNCH", phase="implement", prompt="x", workspace=tmp_path,
        model="test-model", timeout_minutes=1, log_name="launch.log",
    )

    assert result.launch_failure
    assert not result.auth_failure
    event = usage.events()[0]
    assert event["launch_failure"] is True
    assert event["auth_failure"] is False


def test_codex_launch_failure_falls_back_with_launch_failure_reason(tmp_path: Path) -> None:
    launch = AgentResult(
        False, "", "codex could not start: missing", provider="codex",
        launch_failure=True,
    )
    codex = _FakeCodex([launch])
    cursor = _FakeCursor([AgentResult(True, "cursor ok", provider="cursor")])
    router, state, usage = _router(tmp_path, codex, cursor)
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    result = router.implement(task, workspace, 1, 1, None)

    assert result.ok and result.provider == "cursor"
    assert state.get_meta("S1.T1.provider.codex.disabled") == "launch_failure"
    assert state.get(task.id).provider_attempts == {"cursor": 1}
    switch = [e for e in usage.events() if e.get("event_type") == "provider_switch"]
    assert len(switch) == 1
    assert switch[0]["reason"] == "launch_failure"


def test_codex_auth_probe_failure_still_auth_or_cli_unavailable(tmp_path: Path) -> None:
    codex = _FakeCodex(available=False)
    cursor = _FakeCursor([AgentResult(True, "cursor ok", provider="cursor")])
    router, state, _ = _router(tmp_path, codex, cursor)
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    result = router.implement(task, workspace, 1, 1, None)

    assert result.ok and result.provider == "cursor"
    assert state.get_meta("S1.T1.provider.codex.disabled") == "auth_or_cli_unavailable"


def test_provider_runner_resource_failures_do_not_consume_cursor_attempts(tmp_path: Path) -> None:
    """Five resource launches via real ProviderAgentRunner routing; budget survives until block."""
    config, roadmap, state, worktrees, verifier, failures = _init_repo(tmp_path)
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    cursor = _ResourceFailCursor()
    codex = _FakeCodex(available=False)
    runner = ProviderAgentRunner(config, state, codex, cursor, usage)
    assert runner.manages_model_routing is True
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)
    task = roadmap.sprint("s1").tasks["A"]
    base = worktrees.head_commit()

    for expected in range(1, 5):
        outcome = scheduler._run_task(task, base)
        assert outcome.resource_paused
        runtime = state.get("A")
        assert runtime.status == TaskStatus.PENDING
        assert runtime.resource_launch_failures == expected
        assert runtime.provider_attempts.get("cursor", 0) == 0
        assert runner.has_implementation_budget(task)
        assert cursor.calls == expected

    outcome = scheduler._run_task(task, base)
    assert not outcome.ok
    assert not outcome.resource_paused
    runtime = state.get("A")
    assert runtime.status == TaskStatus.BLOCKED
    assert runtime.resource_launch_failures == 5
    assert runtime.provider_attempts.get("cursor", 0) == 0
    assert "out of resources" in (runtime.last_error or "")
    assert cursor.calls == 5


def test_provider_runner_cursor_launch_failure_does_not_consume_attempt(tmp_path: Path) -> None:
    config = _cursor_only_config(tmp_path)
    state = StateStore(tmp_path / "state.json")
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    cursor = _FakeCursor([
        AgentResult(
            False, "", "cursor could not start: missing",
            provider="cursor", launch_failure=True,
        )
    ])
    router = ProviderAgentRunner(config, state, _FakeCodex(available=False), cursor, usage)
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    result = router.implement(task, workspace, 1, 1, None)

    assert result.launch_failure
    assert state.get(task.id).provider_attempts.get("cursor", 0) == 0
    assert router.has_implementation_budget(task)


class _FakeCodex:
    def __init__(self, results: list[AgentResult] | None = None, available: bool = True):
        self.results = list(results or [])
        self.available = available

    def availability(self, **kwargs):
        return self.available, "ok" if self.available else "down"

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        if self.results:
            return self.results.pop(0)
        return AgentResult(True, "ok", provider="codex")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n", provider="codex")

    def plan_change(self, *args, **kwargs):
        return AgentResult(True, "CHANGE_KIND: NONE\n", provider="codex")


class _FakeCursor:
    def __init__(self, results: list[AgentResult] | None = None):
        self.results = list(results or [])
        self.implement_classes: list[str | None] = []

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.implement_classes.append(kwargs.get("model_class_override"))
        if self.results:
            return self.results.pop(0)
        return AgentResult(True, "ok", provider="cursor")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n", provider="cursor")

    def plan_change(self, *args, **kwargs):
        return AgentResult(True, "CHANGE_KIND: NONE\n", provider="cursor")


class _ResourceFailCursor:
    def __init__(self) -> None:
        self.calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.calls += 1
        code = next(iter(WINDOWS_RESOURCE_EXIT_CODES))
        return AgentResult(
            False,
            "",
            f"cursor could not start: the computer is out of resources (exit code {code})",
            provider="cursor",
            transient_error=True,
            resource_launch_failure=True,
        )

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n", provider="cursor")

    def plan_change(self, *args, **kwargs):
        return AgentResult(True, "CHANGE_KIND: NONE\n", provider="cursor")


def _task(tmp_path: Path) -> TaskSpec:
    task_file = tmp_path / "task.md"
    task_file.write_text("# task\n", encoding="utf-8")
    return TaskSpec(id="T1", file=task_file, sprint="S1", stage=1, review=True, allowed_paths=["**"])


def _router(tmp_path: Path, codex: _FakeCodex, cursor: _FakeCursor):
    config = _codex_config(tmp_path, "codex")
    state = StateStore(tmp_path / "state.json")
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    return ProviderAgentRunner(config, state, codex, cursor, usage), state, usage


def _cursor_only_config(tmp_path: Path) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(
        """execution: {watchdog_poll_seconds: 0.01}
worktree: {}
code_map: {command: graphify}
cursor:
  enabled: true
  command: agent
  models: {worker: test-model}
codex: {enabled: false}
providers: {priority: [cursor, codex]}
retry: {sequence: [worker]}
verification: {}
paths: {}
context: {}
doctor: {}
""",
        encoding="utf-8",
    )
    return HarnessConfig.load(path)


def _codex_config(tmp_path: Path, codex_command: str) -> HarnessConfig:
    payload = {
        "execution": {
            "max_parallel_agents": 1,
            "default_max_attempts": 2,
            "default_timeout_minutes": 1,
            "stall_timeout_minutes": 5,
            "watchdog_poll_seconds": 0.1,
        },
        "cursor": {
            "enabled": True,
            "command": "agent",
            "models": {
                "worker": "composer-2.5",
                "hard_worker": "cursor-grok-4.6-high",
                "reviewer": "cursor-grok-4.6-high",
                "escalation": "claude-opus-5-thinking-high",
            },
        },
        "providers": {"priority": ["codex", "cursor"]},
        "codex": {
            "enabled": True,
            "command": codex_command,
            "reasoning_effort": "high",
            "sandbox": "workspace-write",
            "implementation_sequence": [
                {"model": "gpt-5.6-sol", "reasoning_effort": "medium"},
            ],
            "review_model": "gpt-5.6-sol",
            "planner_model": "gpt-5.6-sol",
        },
        "retry": {"sequence": ["worker"]},
        "verification": {"global_task_commands": ["git diff --check"], "stage_commands": []},
        "paths": {"protected": [], "review_required": []},
    }
    path = tmp_path / "harness.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return HarnessConfig.load(path)


def _run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _init_repo(root: Path):
    _run(["git", "init", "-b", "main"], root)
    _run(["git", "config", "user.name", "Test"], root)
    _run(["git", "config", "user.email", "test@example.com"], root)
    _write(root / ".gitignore", ".harness/\n")
    _write(root / "AGENTS.md", "# test\n")
    _write(
        root / "harness.yaml",
        """
execution:
  max_parallel_agents: 1
  default_max_attempts: 2
  default_timeout_minutes: 1
cursor:
  command: agent
  output_format: text
  models: {worker: null, reviewer: null, escalation: null}
codex:
  enabled: false
providers: {priority: [cursor, codex]}
retry: {sequence: [worker]}
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths: {protected: [], review_required: []}
quota: {policy: stop}
capacity: {policy: wait, retry_interval_minutes: 0}
""",
    )
    verify_out = file_exists_command("out.txt")
    _write(root / "tasks/A.md", f"""---
allowed_paths: [out.txt]
verification: [{yaml_quote(verify_out)}]
review: false
---
# A
""")
    _write(root / "tasks/roadmap.yaml", """
project: test
sprints:
  s1:
    tasks:
      A: {file: tasks/A.md, stage: 1, depends_on: []}
    stage_verification:
      "1": []
""")
    _run(["git", "add", "."], root)
    _run(["git", "commit", "-m", "initial"], root)
    config = HarnessConfig.load(root / "harness.yaml")
    from harness.roadmap import Roadmap
    roadmap = Roadmap(root, root / "tasks/roadmap.yaml")
    state = StateStore(root / ".harness/state.json")
    worktrees = WorktreeManager(root, root / ".harness")
    verifier = Verifier(config, root / ".harness/logs", worktrees)
    failures = FailureStore(root / ".harness/failures")
    return config, roadmap, state, worktrees, verifier, failures
