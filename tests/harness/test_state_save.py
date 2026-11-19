from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.models import TaskStatus
from harness.state import StateStore


def _store(tmp_path: Path, sleeps: list[float], tries: int = 20) -> StateStore:
    return StateStore(tmp_path / "state.json", replace_tries=tries, sleep=sleeps.append)


def test_save_retries_permission_error(tmp_path, monkeypatch):
    sleeps: list[float] = []
    store = _store(tmp_path, sleeps)
    real = Path.replace
    calls = {"n": 0}

    def flaky(self, target):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise PermissionError("denied")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", flaky)
    store.set_status("T1", TaskStatus.IMPLEMENTING)
    assert len(sleeps) == 2
    assert "T1" in json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["tasks"]


def test_save_raises_after_tries(tmp_path, monkeypatch):
    sleeps: list[float] = []
    store = _store(tmp_path, sleeps, tries=3)

    def always(self, target):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "replace", always)
    with pytest.raises(PermissionError):
        store.save()
    assert len(sleeps) == 2


def test_load_survives_permission_error_and_truncated_json(tmp_path, monkeypatch):
    sleeps: list[float] = []
    store = _store(tmp_path, sleeps)
    store.set_status("T1", TaskStatus.IMPLEMENTING)
    real = Path.read_text
    calls = {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise PermissionError("denied")
        if calls["n"] == 2:
            return '{"meta": {'
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", flaky)
    store.load()
    assert len(sleeps) == 2
    assert store.get("T1").status == TaskStatus.IMPLEMENTING
