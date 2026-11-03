from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from harness.codex_runner import CodexAgentRunner
from harness.config import HarnessConfig
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.models import TaskSpec, TaskStatus, WINDOWS_RESOURCE_EXIT_CODES
from harness.provider_runner import ProviderAgentRunner
from harness.runner import AgentResult, CursorAgentRunner
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.usage import UsageRecorder
from harness.verifier import Verifier
from tests.portable import file_exists_command, yaml_quote


@pytest.mark.parametrize("launch_error", [
    FileNotFoundError("CLI not found"),
    PermissionError("CLI is not executable"),
    OSError("Invalid executable format"),
])
def test_codex_oserror_is_launch_failure_not_provider_wait(tmp_path: Path, monkeypatch, launch_error):
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    runner = CodexAgentRunner(_codex_config(tmp_path, "codex"), tmp_path / "logs", usage=usage)

    def fail_launch(*args, **kwargs):
        raise launch_error

    monkeypatch.setattr("harness.codex_runner.subprocess.Popen", fail_launch)
    result = runner._invoke(
        task_id="LAUNCH", phase="implement", prompt="x", workspace=tmp_path,
        model="test-model", timeout_minutes=1, log_name="launch.log",
    )

    assert not result.ok
    assert result.launch_failure
    assert result.error == f"codex could not start: {launch_error}"
    assert result.provider == "codex"
    assert result.waiting_cause is None
    assert not result.transient_error
    assert not result.capacity_exhausted
    assert not result.quota_exhausted
    assert (tmp_path / "logs" / "launch.log").read_text(encoding="utf-8") == str(launch_error)


@pytest.mark.parametrize("launch_error", [
    FileNotFoundError("CLI not found"),
    PermissionError("CLI is not executable"),
    OSError("Invalid executable format"),
])
def test_cursor_oserror_is_launch_failure_not_provider_wait(tmp_path: Path, monkeypatch, launch_error):
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    runner = CursorAgentRunner(_cursor_config(tmp_path), tmp_path / "logs", usage=usage)

    def fail_launch(*args, **kwargs):
        raise launch_error

    monkeypatch.setattr("harness.runner.subprocess.Popen", fail_launch)
    result = runner._invoke(
        task_id="LAUNCH", phase="implement", prompt="x", workspace=tmp_path,
        model_class="worker", timeout_minutes=1, log_name="launch.log",
    )

    assert not result.ok
    assert result.launch_failure
    assert result.error == f"cursor could not start: {launch_error}"
    assert result.provider == "cursor"
    assert result.waiting_cause is None
    assert not result.transient_error
    assert not result.capacity_exhausted
    assert not result.quota_exhausted


def test_codex_launch_failure_falls_back_to_cursor_without_consuming_attempt(tmp_path: Path):
    launch = AgentResult(
        False, "", "codex could not start: missing", provider="codex",
        launch_failure=True, auth_failure=True,
    )
    codex = _FakeCodex([launch])
    cursor = _FakeCursor([AgentResult(True, "cursor ok", provider="cursor")])
    router, state, _ = _router(tmp_path, codex, cursor)
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()
    state.get(task.id).attempt = 3
    state.save()

    result = router.implement(task, workspace, 1, 3, None)

    assert result.ok and result.provider == "cursor"
    assert state.get(task.id).attempt == 3
    assert state.get(task.id).provider_attempts == {"cursor": 1}
    assert state.get_meta("S1.T1.provider.codex.disabled") == "auth"


def test_codex_launch_failure_blocks_when_cursor_disabled(tmp_path: Path):
    launch = AgentResult(
        False, "", "codex could not start: missing", provider="codex",
        launch_failure=True, auth_failure=True,
    )
    codex = _FakeCodex([launch])
    cursor = _FakeCursor([AgentResult(True, "should not run", provider="cursor")])
    router, state, _ = _router(tmp_path, codex, cursor)
    router.config.cursor.enabled = False
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    result = router.implement(task, workspace, 1, 1, None)

    assert not result.ok
    assert result.launch_failure
    assert result.provider == "codex"
    assert cursor.implement_classes == []
    assert state.get_meta("S1.T1.provider.codex.disabled") is None


def test_scheduler_blocks_cursor_launch_failure_immediately(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = _init_repo(tmp_path)
    runner = _LaunchFailRunner(provider="cursor")
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)
    sprint = roadmap.sprint("s1")

    assert scheduler.run_sprint(sprint) is False

    runtime = state.get("A")
    assert runtime.status == TaskStatus.BLOCKED
    assert runtime.attempt == 0
    assert "cursor could not start: missing" in (runtime.last_error or "")
    assert "check the program with harness setup-machine" in (runtime.last_error or "")
    assert runtime.waiting_phase is None
    record = failures.get("s1")
    assert record is not None
    assert "cursor could not start: missing" in str(record["message"])
    assert "check the program with harness setup-machine" in str(record["message"])
    assert "harness setup-machine" in str(record["message"])


def test_run_task_launch_failure_records_setup_machine_hint(tmp_path: Path):
    """run-task uses _run_task alone; FailureStore must come from _block_launch_failure."""
    config, roadmap, state, worktrees, verifier, failures = _init_repo(tmp_path)
    runner = _LaunchFailRunner(provider="cursor")
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)
    task = roadmap.sprint("s1").tasks["A"]
    base = worktrees.head_commit()

    outcome = scheduler._run_task(task, base)

    assert not outcome.ok
    assert "cursor could not start: missing" in (outcome.error or "")
    assert "harness setup-machine" in (outcome.error or "")
    runtime = state.get("A")
    assert runtime.status == TaskStatus.BLOCKED
    assert runtime.attempt == 0
    record = failures.get("s1")
    assert record is not None
    assert record["kind"] == "TASK_BLOCKED"
    assert "cursor could not start: missing" in str(record["message"])
    assert "check the program with harness setup-machine" in str(record["message"])


def test_scheduler_cursor_launch_failure_falls_back_to_codex_without_consuming_attempt(
    tmp_path: Path,
):
    """Cursor launch_failure has no auth_failure; fallback is scheduler disable+continue."""
    config, roadmap, state, worktrees, verifier, failures = _init_repo(
        tmp_path, both_providers=True
    )
    runner = _CursorLaunchThenCodexRunner(state)
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)
    task = roadmap.sprint("s1").tasks["A"]
    base = worktrees.head_commit()
    state.get("A").attempt = 0
    state.save()

    outcome = scheduler._run_task(task, base)

    assert outcome.ok
    assert runner.providers == ["cursor", "codex"]
    runtime = state.get("A")
    assert runtime.status == TaskStatus.PASSED
    assert runtime.attempt == 1
    assert state.get_meta("s1.A.provider.cursor.disabled") == "launch_failure"
    assert failures.get("s1") is None


def test_scheduler_blocks_codex_launch_failure_without_cursor(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = _init_repo(tmp_path, codex_only=True)
    runner = _LaunchFailRunner(provider="codex")
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)
    sprint = roadmap.sprint("s1")

    assert scheduler.run_sprint(sprint) is False

    runtime = state.get("A")
    assert runtime.status == TaskStatus.BLOCKED
    assert runtime.attempt == 0
    assert "codex could not start: missing" in (runtime.last_error or "")
    assert "harness setup-machine" in (runtime.last_error or "")
    assert runtime.waiting_phase is None


@pytest.mark.parametrize("exit_code", sorted(WINDOWS_RESOURCE_EXIT_CODES))
def test_windows_resource_exit_is_resource_launch_failure(tmp_path: Path, monkeypatch, exit_code):
    runner = CursorAgentRunner(_cursor_config(tmp_path), tmp_path / "logs")

    class FakeStdin:
        def write(self, data):
            return None

        def flush(self):
            return None

        def close(self):
            return None

    class FakeProc:
        def __init__(self):
            self.stdin = FakeStdin()
            self.stdout = _EmptyStream()
            self.returncode = exit_code

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(runner, "_workspace_signature", lambda workspace: "")
    monkeypatch.setattr("harness.runner.subprocess.Popen", lambda *a, **k: FakeProc())
    result = runner._invoke(
        task_id="RES", phase="implement", prompt="x", workspace=tmp_path,
        model_class="worker", timeout_minutes=1, log_name="res.log",
    )

    assert not result.ok
    assert result.resource_launch_failure
    assert not result.launch_failure
    assert result.waiting_cause is None
    assert f"exit code {exit_code}" in (result.error or "")
    assert "out of resources" in (result.error or "")


def test_scheduler_requeues_resource_launch_then_blocks_after_five(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = _init_repo(tmp_path)
    runner = _ResourceFailRunner(provider="cursor")
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)
    task = roadmap.sprint("s1").tasks["A"]
    base = worktrees.head_commit()

    for expected in range(1, 5):
        outcome = scheduler._run_task(task, base)
        assert outcome.capacity_paused
        assert outcome.resource_paused
        assert outcome.waiting_status is None
        assert not outcome.ok
        runtime = state.get("A")
        assert runtime.status == TaskStatus.PENDING
        assert runtime.waiting_phase == "implement"
        assert runtime.attempt == 0
        assert runtime.resource_launch_failures == expected
        assert runner.calls == expected

    outcome = scheduler._run_task(task, base)
    assert not outcome.ok
    assert not outcome.capacity_paused
    assert not outcome.resource_paused
    runtime = state.get("A")
    assert runtime.status == TaskStatus.BLOCKED
    assert runtime.attempt == 0
    assert runtime.resource_launch_failures == 5
    assert "out of resources" in (runtime.last_error or "")
    assert runner.calls == 5


def test_scheduler_resource_launch_stage_pause_is_not_provider_error(tmp_path: Path):
    """Stage teardown must not stamp WAITING_PROVIDER_ERROR for a resource requeue."""
    config, roadmap, state, worktrees, verifier, failures = _init_repo(tmp_path)
    runner = _ResourceFailRunner(provider="cursor")
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)
    sprint = roadmap.sprint("s1")

    assert scheduler.run_sprint(sprint) is False

    runtime = state.get("A")
    assert runtime.status == TaskStatus.PENDING
    assert runtime.attempt == 0
    assert runtime.resource_launch_failures == 1
    assert runtime.waiting_phase == "implement"
    assert state.get_meta("s1.run_status") == TaskStatus.PENDING.value
    assert state.get_meta("s1.run_status") != TaskStatus.WAITING_PROVIDER_ERROR.value
    record = failures.get("s1")
    assert record is not None
    assert record["kind"] == "CAPACITY_WAIT"
    message = str(record["message"]).lower()
    assert "provider error" not in message
    assert "out of resources" in message


def test_scheduler_review_resource_launch_resumes_without_new_implement(tmp_path: Path):
    config, roadmap, state, worktrees, verifier, failures = _init_repo(tmp_path, review=True)
    runner = _ReviewResourceThenPassRunner(provider="cursor")
    scheduler = Scheduler(tmp_path, config, state, worktrees, runner, verifier, failures=failures)
    sprint = roadmap.sprint("s1")

    assert scheduler.run_sprint(sprint) is False
    runtime = state.get("A")
    assert runtime.status == TaskStatus.PENDING
    assert runtime.waiting_phase == "review"
    assert runtime.attempt == 1
    assert runtime.resource_launch_failures == 1
    assert runner.implement_calls == 1
    assert runner.review_calls == 1
    assert state.get_meta("s1.run_status") != TaskStatus.WAITING_PROVIDER_ERROR.value
    record = failures.get("s1")
    assert record is not None
    assert "provider error" not in str(record["message"]).lower()

    assert scheduler.run_sprint(sprint) is True
    assert state.get("A").status == TaskStatus.DONE
    assert state.get("A").attempt == 1
    assert runner.implement_calls == 1
    assert runner.review_calls == 2


class _EmptyStream:
    def __iter__(self):
        return iter(())


class _LaunchFailRunner:
    manages_model_routing = False

    def __init__(self, *, provider: str):
        self.provider = provider

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        return AgentResult(
            False,
            "",
            f"{self.provider} could not start: missing",
            provider=self.provider,
            launch_failure=True,
        )

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n", provider=self.provider)

    def fallback_after_stalls(self, *args, **kwargs):
        return False


class _CursorLaunchThenCodexRunner:
    """Mirrors scheduler Cursor→Codex fallback: no auth_failure, only launch_failure."""

    manages_model_routing = False

    def __init__(self, state: StateStore):
        self.state = state
        self.providers: list[str] = []

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        key = f"{task.sprint}.{task.id}.provider.cursor.disabled"
        if self.state.get_meta(key):
            self.providers.append("codex")
            (workspace / "out.txt").write_text("ok\n", encoding="utf-8")
            return AgentResult(True, "codex ok", provider="codex")
        self.providers.append("cursor")
        return AgentResult(
            False,
            "",
            "cursor could not start: missing",
            provider="cursor",
            launch_failure=True,
        )

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n", provider="codex")

    def fallback_after_stalls(self, *args, **kwargs):
        return False


class _ResourceFailRunner:
    manages_model_routing = False

    def __init__(self, *, provider: str):
        self.provider = provider
        self.calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.calls += 1
        code = next(iter(WINDOWS_RESOURCE_EXIT_CODES))
        return AgentResult(
            False,
            "",
            f"{self.provider} could not start: the computer is out of resources (exit code {code})",
            provider=self.provider,
            transient_error=True,
            resource_launch_failure=True,
        )

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n", provider=self.provider)

    def fallback_after_stalls(self, *args, **kwargs):
        return False


class _ReviewResourceThenPassRunner:
    manages_model_routing = False

    def __init__(self, *, provider: str):
        self.provider = provider
        self.implement_calls = 0
        self.review_calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.implement_calls += 1
        (workspace / "out.txt").write_text("ok\n", encoding="utf-8")
        return AgentResult(True, "ok", provider=self.provider)

    def review(self, *args, **kwargs):
        self.review_calls += 1
        if self.review_calls == 1:
            code = next(iter(WINDOWS_RESOURCE_EXIT_CODES))
            return AgentResult(
                False,
                "",
                f"{self.provider} could not start: the computer is out of resources (exit code {code})",
                provider=self.provider,
                transient_error=True,
                resource_launch_failure=True,
            )
        return AgentResult(True, "VERDICT: PASS\n", provider=self.provider)

    def fallback_after_stalls(self, *args, **kwargs):
        return False


class _FakeCodex:
    def __init__(self, results: list[AgentResult] | None = None, available: bool = True):
        self.results = list(results or [])
        self.available = available
        self.models: list[str] = []

    def availability(self, **kwargs):
        return self.available, "ok" if self.available else "down"

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, *, model, reasoning_effort=None, env=None, **kwargs):
        self.models.append(model)
        if self.results:
            return self.results.pop(0)
        return AgentResult(True, "ok", model=model, provider="codex")

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


def _task(tmp_path: Path) -> TaskSpec:
    task_file = tmp_path / "task.md"
    task_file.write_text("# task\n", encoding="utf-8")
    return TaskSpec(id="T1", file=task_file, sprint="S1", stage=1, review=True, allowed_paths=["**"])


def _router(tmp_path: Path, codex: _FakeCodex, cursor: _FakeCursor):
    config = _codex_config(tmp_path, "codex")
    state = StateStore(tmp_path / "state.json")
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    return ProviderAgentRunner(config, state, codex, cursor, usage), state, usage


def _cursor_config(tmp_path: Path, command: str = "agent") -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(
        f"""execution: {{watchdog_poll_seconds: 0.01}}
worktree: {{}}
code_map: {{command: graphify}}
cursor:
  command: {yaml_quote(command)}
  models: {{worker: test-model}}
verification: {{}}
paths: {{}}
context: {{}}
doctor: {{}}
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


def _init_repo(
    root: Path,
    *,
    codex_only: bool = False,
    both_providers: bool = False,
    review: bool = False,
):
    _run(["git", "init", "-b", "main"], root)
    _run(["git", "config", "user.name", "Test"], root)
    _run(["git", "config", "user.email", "test@example.com"], root)
    _write(root / ".gitignore", ".harness/\n")
    _write(root / "AGENTS.md", "# test\n")
    if codex_only:
        harness = """
execution:
  max_parallel_agents: 1
  default_max_attempts: 2
  default_timeout_minutes: 1
cursor:
  enabled: false
  command: agent
  models: {worker: null, reviewer: null, escalation: null}
codex:
  enabled: true
  command: codex
  implementation_sequence: [{model: gpt-test, reasoning_effort: medium}]
  review_model: gpt-test
  planner_model: gpt-test
providers: {priority: [codex, cursor]}
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths: {protected: [], review_required: []}
quota: {policy: stop}
capacity: {policy: wait, retry_interval_minutes: 0}
"""
    elif both_providers:
        harness = """
execution:
  max_parallel_agents: 1
  default_max_attempts: 2
  default_timeout_minutes: 1
cursor:
  enabled: true
  command: agent
  output_format: text
  models: {worker: null, reviewer: null, escalation: null}
codex:
  enabled: true
  command: codex
  implementation_sequence: [{model: gpt-test, reasoning_effort: medium}]
  review_model: gpt-test
  planner_model: gpt-test
providers: {priority: [cursor, codex]}
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths: {protected: [], review_required: []}
quota: {policy: stop}
capacity: {policy: wait, retry_interval_minutes: 0}
"""
    else:
        harness = """
execution:
  max_parallel_agents: 1
  default_max_attempts: 2
  default_timeout_minutes: 1
cursor:
  command: agent
  output_format: text
  models: {worker: null, reviewer: null, escalation: null}
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths: {protected: [], review_required: []}
quota: {policy: stop}
capacity: {policy: wait, retry_interval_minutes: 0}
"""
    _write(root / "harness.yaml", harness)
    verify_out = file_exists_command("out.txt")
    review_flag = "true" if review else "false"
    _write(root / "tasks/A.md", f"""---
allowed_paths: [out.txt]
verification: [{yaml_quote(verify_out)}]
review: {review_flag}
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
