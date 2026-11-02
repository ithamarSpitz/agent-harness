from __future__ import annotations

import ctypes
import os
import shutil
from collections.abc import Mapping
from typing import Any, TextIO


_STD_OUTPUT_HANDLE = -11
_ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004

_CYAN = "\x1b[36m"
_GREEN = "\x1b[32m"
_YELLOW = "\x1b[33m"
_RED = "\x1b[31m"

_RUNNING_STATUSES = {"IMPLEMENTING", "CHECKING", "REVIEWING", "MERGING"}
_PASSED_STATUSES = {"PASSED", "DONE"}
_RETRY_STATUSES = {"RETRY", "BLOCKED"}


def enable_ansi() -> bool:
    """Enable ANSI escape processing for the current console output stream."""
    if os.name != "nt":
        return True

    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(_STD_OUTPUT_HANDLE)
        if handle in (0, -1):
            return False

        mode = ctypes.c_ulong()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if not kernel32.SetConsoleMode(handle, mode.value | _ENABLE_VIRTUAL_TERMINAL_PROCESSING):
            return False
    except Exception:
        return False
    return True


def supports_unicode(stream: TextIO) -> bool:
    """Return whether *stream* can represent the symbols used by the console."""
    encoding = getattr(stream, "encoding", None)
    if not encoding:
        return False
    try:
        "✓←".encode(encoding)
    except (LookupError, UnicodeEncodeError):
        return False
    return True


def color_for(status: Any, environment: Mapping[str, str] | None = None) -> str:
    """Return the ANSI foreground color for a task status."""
    env = os.environ if environment is None else environment
    if "NO_COLOR" in env:
        return ""

    value = getattr(status, "value", status)
    name = str(value).upper()
    if name in _RUNNING_STATUSES:
        return _CYAN
    if name in _PASSED_STATUSES:
        return _GREEN
    if name.startswith("WAITING_"):
        return _YELLOW
    if name in _RETRY_STATUSES:
        return _RED
    return ""


class Screen:
    """A small in-place terminal drawing surface."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream
        self._height = 0

    def draw(self, lines: list[str]) -> None:
        if self._height:
            self.stream.write(f"\x1b[{self._height}A")
        for line in lines:
            self.stream.write(line)
            self.stream.write("\n")
        self.stream.write("\x1b[J")
        self.stream.flush()
        self._height = len(lines)

    def size(self) -> os.terminal_size:
        return shutil.get_terminal_size()

    def close(self) -> None:
        self.stream.flush()

