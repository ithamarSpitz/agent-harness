from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
import yaml

from harness.config import HarnessConfig
from harness.failure import FailureStore
from harness.git_worktree import WorktreeManager
from harness.headroom import Headroom
from harness.models import TaskStatus
from harness.provider_runner import ProviderAgentRunner
from harness.roadmap import Roadmap
from harness.runner import AgentResult
from harness.scheduler import Scheduler
from harness.state import StateStore
from harness.verifier import Verifier
from tests.portable import file_exists_command, yaml_quote


def _run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


class _FakeProvider:
    def __init__(self, log_dir: Path | None = None) -> None:
        self.log_dir = log_dir
        self.implement_calls = 0

    def availability(self, **_kwargs):
        return True, "ok"

    def implement(self, *_args, **_kwargs):
        self.implement_calls += 1
        return AgentResult(True, "unexpected call")

    def review(self, *_args, **_kwargs):
        return AgentResult(True, "VERDICT: PASS")


def _setup(
    root: Path, *, cursor_enabled: bool = True
) -> tuple[
    HarnessConfig,
    Roadmap,
    StateStore,
    Scheduler,
    ProviderAgentRunner,
    int,
]:
    _run(["git", "init", "-b", "main"], root)
    _run(["git", "config", "user.name", "Test"], root)
    _run(["git", "config", "user.email", "test@example.com"], root)
    (root / ".gitignore").write_text(".harness/\n", encoding="utf-8")
    (root / "AGENTS.md").write_text("# test\n", encoding="utf-8")
    (root / "ARCHITECTURE.md").write_text("# test\n", encoding="utf-8")
    config_data = {
        "execution": {"max_parallel_agents": 1, "default_timeout_minutes": 1},
        "agy": {"enabled": False},
        "codex": {
            "enabled": True,
            "implementation_sequence": [
                {"model": "codex-model", "reasoning_effort": "medium"}
            ],
        },
        "cursor": {
            "enabled": cursor_enabled,
            "auto_when_exhausted": False,
            "models": {"worker": "cursor-model"},
        },
        "claude": {
            "enabled": True,
            "implementation_sequence": [{"model": "sonnet", "effort": "medium"}],
            "reserve": {"min_percent": 60, "max_percent": 90},
        },
        "providers": {"priority": ["codex", "claude", "cursor", "agy"]},
        "retry": {"sequence": ["worker"]},
        "verification": {"global_task_commands": [], "stage_commands": []},
        "paths": {"protected": [], "review_required": []},
        "quota": {"policy": "stop", "probe_interval_minutes": 60},
    }
    (root / "harness.yaml").write_text(
        yaml.safe_dump(config_data, sort_keys=False), encoding="utf-8"
    )
    verify = file_exists_command("out.txt")
    tasks = root / "tasks"
    tasks.mkdir()
    (tasks / "A.md").write_text(
        "---\n"
        "allowed_paths: [out.txt]\n"
        f"verification: [{yaml_quote(verify)}]\n"
        "review: false\n"
        "---\n"
        "# A\n",
        encoding="utf-8",
    )
    (tasks / "roadmap.yaml").write_text(
        "project: test\n"
        "sprints:\n"
        "  s1:\n"
        "    tasks:\n"
        "      A: {file: tasks/A.md, stage: 1, depends_on: []}\n",
        encoding="utf-8",
    )
    _run(["git", "add", "."], root)
    _run(["git", "commit", "-m", "initial"], root)

    config = HarnessConfig.load(root / "harness.yaml")
    roadmap = Roadmap(root, tasks / "roadmap.yaml")
    state = StateStore(root / ".harness/state.json")
    worktrees = WorktreeManager(root, root / ".harness")
    verifier = Verifier(config, root / ".harness/logs", worktrees)
    failures = FailureStore(root / ".harness/failures")
    now = int(time.time())
    snapshot = {
        "codex": [Headroom("codex", 100, now + 3600, "five_hour", True)],
        "cursor": [
            Headroom("cursor", 100, now + 7200, "composer_grok", True),
            Headroom("cursor", 100, now + 7200, "other_models", True),
        ],
        "claude": [
            Headroom("claude", 80, now + 10800, "five_hour", True),
            Headroom("claude", 20, now + 604800, "weekly", True),
        ],
    }
    codex = _FakeProvider(root / ".harness/logs")
    cursor = _FakeProvider()
    claude = _FakeProvider()
    runner = ProviderAgentRunner(
        config,
        state,
        codex,
        cursor,
        claude=claude,
        headroom_reader=lambda: snapshot,
    )
    scheduler = Scheduler(
        root, config, state, worktrees, runner, verifier, failures=failures
    )
    return config, roadmap, state, scheduler, runner, now


def test_temporarily_unavailable_providers_wait_without_spending_attempt(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _config, roadmap, state, scheduler, _runner, now = _setup(tmp_path)

    assert not scheduler.run_sprint(roadmap.sprint("s1"))

    runtime = state.get("A")
    assert runtime.status == TaskStatus.WAITING_QUOTA
    assert runtime.attempt == 0
    assert scheduler.failures.get("s1")["kind"] == "QUOTA_WAIT"
    assert state.get_meta("s1.A.routing_quota_reset_at") == pytest.approx(now + 3600)
    assert "Waiting (quota, none): A" in capsys.readouterr().out


def test_exhausted_provider_attempt_sequences_block_without_calling_an_agent(
    tmp_path: Path,
) -> None:
    _config, roadmap, state, scheduler, runner, _now = _setup(tmp_path)
    runtime = state.get("A")
    runtime.provider_attempts = {"codex": 1, "cursor": 1, "claude": 1}
    state.save()

    assert not scheduler.run_sprint(roadmap.sprint("s1"))

    runtime = state.get("A")
    assert runtime.status == TaskStatus.BLOCKED
    assert runtime.attempt == 0
    assert runtime.last_error == "Attempt budget exhausted"
    assert not runner.has_implementation_budget(roadmap.sprint("s1").tasks["A"])


def test_quota_disabled_codex_and_capped_claude_still_wait(
    tmp_path: Path,
) -> None:
    _config, roadmap, state, scheduler, _runner, now = _setup(
        tmp_path, cursor_enabled=False
    )
    state.set_meta("s1.A.provider.codex.disabled", "quota:5h")

    assert not scheduler.run_sprint(roadmap.sprint("s1"))

    runtime = state.get("A")
    assert runtime.status == TaskStatus.WAITING_QUOTA
    assert runtime.attempt == 0
    assert state.get_meta("s1.A.routing_quota_reset_at") == pytest.approx(now + 10800)
