from __future__ import annotations

import harness.quota as quota


def _reset_keep_awake_state() -> None:
    quota.KeepAwake._depth = 0
    quota.KeepAwake._windows_active = False
    quota.KeepAwake._unix_proc = None


def test_macos_nested_context_starts_helper_once(monkeypatch):
    events: list[str] = []

    class FakeProc:
        pass

    def fake_start(**kwargs):
        events.append("start")
        return FakeProc()

    def fake_stop(proc) -> None:
        events.append("stop")

    monkeypatch.setattr(quota.os, "name", "posix")
    monkeypatch.setattr(quota.sys, "platform", "darwin")
    monkeypatch.setattr(quota, "start_macos_keep_awake", fake_start)
    monkeypatch.setattr(quota, "stop_unix_keep_awake", fake_stop)
    _reset_keep_awake_state()

    with quota.KeepAwake(True):
        assert events == ["start"]
        with quota.KeepAwake(True):
            assert events == ["start"]
    assert events == ["start", "stop"]


def test_linux_with_systemd_inhibit_nested_starts_once(monkeypatch):
    events: list[str] = []

    class FakeProc:
        pass

    def fake_start(**kwargs):
        events.append("start")
        return FakeProc()

    def fake_stop(proc) -> None:
        events.append("stop")

    monkeypatch.setattr(quota.os, "name", "posix")
    monkeypatch.setattr(quota.sys, "platform", "linux")
    monkeypatch.setattr(quota, "start_linux_keep_awake", fake_start)
    monkeypatch.setattr(quota, "stop_unix_keep_awake", fake_stop)
    _reset_keep_awake_state()

    with quota.KeepAwake(True):
        with quota.KeepAwake(True):
            pass
    assert events == ["start", "stop"]


def test_linux_without_systemd_inhibit_does_nothing(monkeypatch):
    popen_calls: list[object] = []

    def fake_popen(*args, **kwargs):
        popen_calls.append(args)
        raise AssertionError("should not spawn a helper process")

    monkeypatch.setattr(quota.os, "name", "posix")
    monkeypatch.setattr(quota.sys, "platform", "linux")
    monkeypatch.setattr(quota.shutil, "which", lambda _: None)
    monkeypatch.setattr(quota.subprocess, "Popen", fake_popen)
    stop_calls: list[object] = []
    monkeypatch.setattr(quota, "stop_unix_keep_awake", lambda proc: stop_calls.append(proc))
    _reset_keep_awake_state()

    with quota.KeepAwake(True):
        assert quota.KeepAwake._depth == 0
    assert popen_calls == []
    assert stop_calls == []


def test_macos_start_failure_logs_warning_and_continues(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(quota.os, "name", "posix")
    monkeypatch.setattr(quota.sys, "platform", "darwin")

    def exploding_start(**kwargs):
        raise OSError("no caffeinate")

    monkeypatch.setattr(quota, "start_macos_keep_awake", exploding_start)
    _reset_keep_awake_state()

    with caplog.at_level(logging.WARNING, logger="harness.quota"):
        with quota.KeepAwake(True):
            pass

    assert any("keep-awake helper failed" in record.message for record in caplog.records)


def test_linux_stop_failure_logs_warning_and_continues(monkeypatch, caplog):
    import logging

    class FakeProc:
        pass

    monkeypatch.setattr(quota.os, "name", "posix")
    monkeypatch.setattr(quota.sys, "platform", "linux")
    monkeypatch.setattr(quota, "start_linux_keep_awake", lambda: FakeProc())

    def exploding_stop(proc) -> None:
        raise OSError("could not stop helper")

    monkeypatch.setattr(quota, "stop_unix_keep_awake", exploding_stop)
    _reset_keep_awake_state()

    with caplog.at_level(logging.WARNING, logger="harness.quota"):
        with quota.KeepAwake(True):
            pass

    assert quota.KeepAwake._unix_proc is None
    assert sum("keep-awake helper failed" in record.message for record in caplog.records) == 1


def test_linux_start_linux_keep_awake_resolves_inhibit(monkeypatch):
    captured: dict[str, object] = {}

    class FakeProc:
        pass

    def fake_which(name: str) -> str | None:
        if name == "systemd-inhibit":
            return "/usr/bin/systemd-inhibit"
        return None

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return FakeProc()

    proc = quota.start_linux_keep_awake(which=fake_which, popen=fake_popen)
    assert proc is not None
    assert captured["args"] == [
        "/usr/bin/systemd-inhibit",
        "--what=idle:sleep",
        "--why=agent harness run",
        "sleep",
        "infinity",
    ]


def test_macos_start_passes_harness_pid(monkeypatch):
    captured: dict[str, object] = {}

    class FakeProc:
        pass

    def fake_popen(args, **kwargs):
        captured["args"] = args
        return FakeProc()

    monkeypatch.setattr(quota.os, "getpid", lambda: 4242)
    proc = quota.start_macos_keep_awake(popen=fake_popen)
    assert proc is not None
    assert captured["args"] == ["caffeinate", "-i", "-w", "4242"]
