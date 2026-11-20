from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.adapters import kiro as kiro_mod
from harness.adapters.base import CallContext
from harness.adapters.kiro import KiroAdapter
from harness.agent_runner import AgentResult
from harness.config import KiroConfig


class FakeRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    def _rec(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return AgentResult(True, "ok")

    def implement(self, *a, **k):
        return self._rec("implement", *a, **k)

    def review(self, *a, **k):
        return self._rec("review", *a, **k)

    def plan_change(self, *a, **k):
        return self._rec("plan_change", *a, **k)

    def brief_check(self, *a, **k):
        return self._rec("brief_check", *a, **k)


def make(runner, **kw):
    return KiroAdapter(runner, SimpleNamespace(kiro=KiroConfig(**kw)))


def test_enabled():
    assert make(FakeRunner(), enabled=True).enabled()
    assert not make(FakeRunner()).enabled()
    assert not make(None, enabled=True).enabled()


def test_levels_and_budget():
    adapter = make(FakeRunner())
    levels = adapter.levels()
    assert (levels.worker, levels.hard_worker, levels.escalation) == (
        "glm-5", "claude-sonnet-4.5", "claude-sonnet-4.5")
    assert adapter.implementation_budget() == 3
    one = make(FakeRunner(), implementation_sequence=["auto"], escalation_model="x")
    levels = one.levels()
    assert (levels.worker, levels.hard_worker, levels.escalation) == ("auto", "auto", "x")
    assert one.implementation_budget() == 1


def _fake_run(whoami):
    def run(cmd, **kwargs):
        if cmd[1] == "--version":
            return subprocess.CompletedProcess(cmd, 0, "kiro-cli 3.1.0\n", "")
        return whoami(cmd)
    return run


def test_find_signed_in(monkeypatch):
    monkeypatch.setattr(kiro_mod.subprocess, "run", _fake_run(
        lambda c: subprocess.CompletedProcess(
            c, 0, "Logged in with Builder ID\nEmail: someone@example.com\n", "")))
    found = make(FakeRunner()).find()
    assert (found.installed, found.version, found.signed_in) == (True, "kiro-cli 3.1.0", True)
    assert "someone@example.com" not in repr(found)


def test_find_not_signed_in(monkeypatch):
    monkeypatch.setattr(kiro_mod.subprocess, "run", _fake_run(
        lambda c: subprocess.CompletedProcess(c, 1, "", "Email: someone@example.com")))
    found = make(FakeRunner()).find()
    assert found.signed_in is False
    assert "someone@example.com" not in repr(found)


def test_find_oserror(monkeypatch):
    def boom(c):
        raise OSError("Email: someone@example.com")
    monkeypatch.setattr(kiro_mod.subprocess, "run", _fake_run(boom))
    found = make(FakeRunner()).find()
    assert found.signed_in is None and found.detail == "OSError"
    assert "someone@example.com" not in repr(found)


def test_delegation():
    runner = FakeRunner()
    adapter = make(runner)
    task, ws, cb = object(), Path("."), lambda pid: None
    ctx = CallContext(task, ws, 5, 1, 2, "prev", {"A": "1"}, "worker", True, cb)
    adapter.implement(ctx)
    adapter.review(task, ws, "base", "sum", 5, env=None, code_map_offered=True, on_process_start=cb)
    adapter.plan_change(task, ws, "fail", 5, model_class="reviewer", env=None)
    adapter.brief_check(task, ws, "brief", 5, env={"A": "1"})
    assert runner.calls == [
        ("implement", (task, ws, 5, 3, "prev"), {
            "env": {"A": "1"}, "model_class_override": "worker",
            "code_map_offered": True, "on_process_start": cb}),
        ("review", (task, ws, "base", "sum", 5), {
            "env": None, "code_map_offered": True, "on_process_start": cb}),
        ("plan_change", (task, ws, "fail", 5), {"model_class": "reviewer", "env": None}),
        ("brief_check", (task, ws, "brief", 5), {"env": {"A": "1"}}),
    ]


def test_missing_runner():
    ctx = CallContext(object(), Path("."), 5, 1, 0, None, None, "worker", False, None)
    with pytest.raises(RuntimeError, match="kiro runner is not available"):
        make(None, enabled=True).implement(ctx)
