from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from harness import command_locate


@pytest.fixture(autouse=True)
def clear_locate_cache() -> None:
    command_locate.clear_locate_cache()
    yield
    command_locate.clear_locate_cache()


def test_locate_finds_command_on_process_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        command_locate.shutil,
        "which",
        lambda command, path=None: "/current/bin/tool" if path is None else None,
    )

    assert command_locate.locate("tool") == "/current/bin/tool"


def test_locate_finds_command_on_faked_registry_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "late-tool.exe"
    executable.write_bytes(b"")
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(command_locate.sys, "platform", "win32")
    monkeypatch.setattr(command_locate, "_registry_path", lambda: str(tmp_path))

    assert command_locate.locate("late-tool.exe") == str(executable)


def test_locate_returns_none_when_command_is_not_anywhere(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(command_locate.sys, "platform", "win32")
    monkeypatch.setattr(command_locate, "_registry_path", lambda: os.devnull)

    assert command_locate.locate("missing-tool") is None


def test_locate_retries_registry_after_a_miss(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "late-tool.exe"
    executable.write_bytes(b"")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    registry_path = os.devnull
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(command_locate.sys, "platform", "win32")
    monkeypatch.setattr(command_locate, "_registry_path", lambda: registry_path)

    assert command_locate.locate("late-tool.exe") is None

    registry_path = str(tmp_path)
    assert command_locate.locate("late-tool.exe") == str(executable)


def test_locate_caches_a_registry_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "cached-tool.exe"
    executable.write_bytes(b"")
    executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
    registry_reads = 0
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(command_locate.sys, "platform", "win32")

    def registry_path() -> str:
        nonlocal registry_reads
        registry_reads += 1
        return str(tmp_path)

    monkeypatch.setattr(command_locate, "_registry_path", registry_path)

    assert command_locate.locate("cached-tool.exe") == str(executable)
    assert command_locate.locate("cached-tool.exe") == str(executable)
    assert registry_reads == 1


def test_locate_never_reads_registry_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(command_locate.sys, "platform", "linux")

    def fail_if_called() -> str:
        raise AssertionError("registry must not be read")

    monkeypatch.setattr(command_locate, "_registry_path", fail_if_called)

    assert command_locate.locate("missing-tool") is None
