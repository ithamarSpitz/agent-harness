from __future__ import annotations

import io
import os
from types import SimpleNamespace

import pytest

from harness import terminal
from harness.models import TaskStatus


class EncodedStringIO(io.StringIO):
    def __init__(self, encoding: str) -> None:
        super().__init__()
        self._encoding = encoding

    @property
    def encoding(self) -> str:
        return self._encoding


def test_screen_redraws_previous_block_and_clears_leftovers() -> None:
    stream = io.StringIO()
    screen = terminal.Screen(stream)

    screen.draw(["first", "second"])
    screen.draw(["replacement"])

    assert stream.getvalue() == "first\nsecond\n\x1b[J\x1b[2Areplacement\n\x1b[J"


def test_screen_size_uses_terminal_size(monkeypatch: pytest.MonkeyPatch) -> None:
    expected = os.terminal_size((101, 37))
    monkeypatch.setattr(terminal.shutil, "get_terminal_size", lambda: expected)

    assert terminal.Screen(io.StringIO()).size() == expected


def test_screen_close_flushes_below_drawn_block() -> None:
    stream = io.StringIO()
    screen = terminal.Screen(stream)
    screen.draw(["line"])

    screen.close()

    assert stream.getvalue().endswith("line\n\x1b[J")


@pytest.mark.parametrize(
    ("status", "color"),
    [
        (TaskStatus.IMPLEMENTING, "\x1b[36m"),
        ("CHECKING", "\x1b[36m"),
        ("REVIEWING", "\x1b[36m"),
        ("MERGING", "\x1b[36m"),
        ("PASSED", "\x1b[32m"),
        ("DONE", "\x1b[32m"),
        ("WAITING_QUOTA", "\x1b[33m"),
        ("WAITING_NETWORK", "\x1b[33m"),
        ("RETRY", "\x1b[31m"),
        ("BLOCKED", "\x1b[31m"),
        ("UNKNOWN", ""),
    ],
)
def test_color_for_status_groups(status: object, color: str) -> None:
    assert terminal.color_for(status, {}) == color


def test_no_color_disables_known_status_color() -> None:
    assert terminal.color_for("DONE", {"NO_COLOR": ""}) == ""


@pytest.mark.parametrize(("encoding", "expected"), [("utf-8", True), ("ascii", False)])
def test_supports_unicode(encoding: str, expected: bool) -> None:
    assert terminal.supports_unicode(EncodedStringIO(encoding)) is expected


def test_enable_ansi_succeeds_without_console_calls_on_posix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terminal.os, "name", "posix")

    assert terminal.enable_ansi() is True


@pytest.mark.parametrize("failed_call", ["handle", "get_mode", "set_mode"])
def test_enable_ansi_returns_false_when_console_call_fails(
    monkeypatch: pytest.MonkeyPatch, failed_call: str
) -> None:
    class FakeKernel32:
        def GetStdHandle(self, _kind: int) -> int:
            return -1 if failed_call == "handle" else 10

        def GetConsoleMode(self, _handle: int, mode: object) -> int:
            if failed_call == "get_mode":
                return 0
            mode._obj.value = 1  # type: ignore[attr-defined]
            return 1

        def SetConsoleMode(self, _handle: int, _mode: int) -> int:
            return 0 if failed_call == "set_mode" else 1

    monkeypatch.setattr(terminal.os, "name", "nt")
    monkeypatch.setattr(terminal.ctypes, "windll", SimpleNamespace(kernel32=FakeKernel32()), raising=False)

    assert terminal.enable_ansi() is False


def test_enable_ansi_preserves_existing_console_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    modes: list[int] = []

    class FakeKernel32:
        def GetStdHandle(self, _kind: int) -> int:
            return 10

        def GetConsoleMode(self, _handle: int, mode: object) -> int:
            mode._obj.value = 0x20  # type: ignore[attr-defined]
            return 1

        def SetConsoleMode(self, _handle: int, mode: int) -> int:
            modes.append(mode)
            return 1

    monkeypatch.setattr(terminal.os, "name", "nt")
    monkeypatch.setattr(terminal.ctypes, "windll", SimpleNamespace(kernel32=FakeKernel32()), raising=False)

    assert terminal.enable_ansi() is True
    assert modes == [0x24]
