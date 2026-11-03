from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from harness.agent_runner import AgentResult
from harness.config import HarnessConfig
from harness.git_worktree import WorktreeManager
from harness.models import TaskSpec
from harness.provider_runner import ProviderAgentRunner
from harness.state import StateStore


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True)


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _task(root: Path) -> TaskSpec:
    task_file = root / "task.md"
    task_file.write_text("# task\n", encoding="utf-8")
    return TaskSpec(id="T1", file=task_file, sprint="S1", stage=1, review=True, allowed_paths=["**"])


def _init_git_worktree(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-b", "main"], repo)
    run(["git", "config", "user.name", "Test"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    write(repo / "tracked.txt", "seed\n")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "seed"], repo)
    return repo


def _base_yaml(tmp_path: Path, *, ui_review: dict | None = None) -> Path:
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
            "command": "codex",
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


class _FakeCodex:
    def __init__(self, log_dir: Path, *, on_implement=None) -> None:
        self.log_dir = log_dir
        self.implement_calls = 0
        self._on_implement = on_implement

    def availability(self, refresh: bool = False):
        return True, "ok"

    def implement(self, *args, **kwargs):
        self.implement_calls += 1
        if self._on_implement is not None:
            self._on_implement()
        return AgentResult(True, "codex implement ok", provider="codex")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n", provider="codex")


class _FakeCursor:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.implement_calls = 0

    def implement(self, *args, **kwargs):
        self.implement_calls += 1
        return AgentResult(True, "cursor implement ok", provider="cursor")

    def review(self, *args, **kwargs):
        return AgentResult(True, "VERDICT: PASS\n", provider="cursor")


def _router(
    repo: Path,
    *,
    ui_review: dict | None,
    on_implement=None,
) -> tuple[ProviderAgentRunner, _FakeCodex, _FakeCursor, Path]:
    config = HarnessConfig.load(_base_yaml(repo, ui_review=ui_review))
    log_dir = repo / ".harness/logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    state = StateStore(repo / ".harness/state.json")
    codex = _FakeCodex(log_dir, on_implement=on_implement)
    cursor = _FakeCursor(log_dir)
    return ProviderAgentRunner(config, state, codex, cursor), codex, cursor, log_dir


def test_implement_cleanup_stops_new_port_listener_leaves_preexisting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    repo = _init_git_worktree(tmp_path)
    manager = WorktreeManager(repo, repo / ".harness")
    _, worktree = manager.create("T1", "HEAD")

    pre_existing = {101: "pre-existing listener"}
    after_call = {101: "pre-existing listener", 202: "new listener from implementer"}
    live = {"snapshot": True}

    def fake_snapshot(ports):
        assert ports == [3000]
        return dict(pre_existing)

    def fake_collect(ports, _protected):
        assert ports == [3000]
        if live["snapshot"]:
            return dict(pre_existing)
        return dict(after_call)

    stopped: list[int] = []

    monkeypatch.setattr("harness.provider_runner.snapshot_port_listeners", fake_snapshot)
    monkeypatch.setattr("harness.ui_review._collect_processes_for_ports", fake_collect)
    monkeypatch.setattr("harness.ui_review._protected_process_ids", lambda: {999999})
    monkeypatch.setattr("harness.ui_review._collect_processes_for_worktree", lambda *_a: {})
    monkeypatch.setattr("harness.ui_review._process_is_alive", lambda _pid: True)
    monkeypatch.setattr(
        "harness.ui_review._stop_discovered_pid",
        lambda pid, _prot: stopped.append(pid),
    )

    def leave_listener():
        live["snapshot"] = False

    router, codex, cursor, log_dir = _router(
        repo,
        ui_review={
            "enabled": True,
            "start_commands": ["npm start"],
            "url": "http://localhost:3000",
            "ports": [3000],
        },
        on_implement=leave_listener,
    )
    result = router.implement(_task(repo), worktree, 1, 1, None)
    assert result.ok
    assert codex.implement_calls == 1
    assert cursor.implement_calls == 0
    assert stopped == [202]
    cleanup_log = log_dir / "T1-ui-review-cleanup.log"
    assert cleanup_log.is_file()
    assert "stopped pid=202" in cleanup_log.read_text(encoding="utf-8")


def test_implement_skips_cleanup_when_ui_review_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    repo = _init_git_worktree(tmp_path)
    manager = WorktreeManager(repo, repo / ".harness")
    _, worktree = manager.create("T1", "HEAD")

    calls: list[str] = []

    def boom_snapshot(*_args, **_kwargs):
        calls.append("snapshot")
        raise AssertionError("snapshot must not run when ui_review is disabled")

    def boom_cleanup(*_args, **_kwargs):
        calls.append("cleanup")
        raise AssertionError("cleanup must not run when ui_review is disabled")

    monkeypatch.setattr("harness.provider_runner.snapshot_port_listeners", boom_snapshot)
    monkeypatch.setattr("harness.provider_runner.cleanup_ui_review_processes", boom_cleanup)

    router, codex, _, _ = _router(repo, ui_review={"enabled": False})
    result = router.implement(_task(repo), worktree, 1, 1, None)
    assert result.ok
    assert codex.implement_calls == 1
    assert calls == []


def test_implement_cleanup_failure_does_not_fail_task(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    repo = _init_git_worktree(tmp_path)
    manager = WorktreeManager(repo, repo / ".harness")
    _, worktree = manager.create("T1", "HEAD")

    monkeypatch.setattr(
        "harness.provider_runner.snapshot_port_listeners",
        lambda _ports: {},
    )

    def boom_cleanup(**_kwargs):
        raise RuntimeError("simulated cleanup failure")

    monkeypatch.setattr("harness.provider_runner.cleanup_ui_review_processes", boom_cleanup)

    router, codex, _, log_dir = _router(
        repo,
        ui_review={
            "enabled": True,
            "start_commands": ["npm start"],
            "url": "http://localhost:3000",
            "ports": [3000],
        },
    )
    result = router.implement(_task(repo), worktree, 1, 1, None)
    assert result.ok
    assert codex.implement_calls == 1
    cleanup_log = log_dir / "T1-ui-review-cleanup.log"
    assert cleanup_log.is_file()
    text = cleanup_log.read_text(encoding="utf-8")
    assert "cleanup failed" in text
    assert "RuntimeError" in text
