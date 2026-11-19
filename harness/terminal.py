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
_ANSI_CSI_PREFIX = "\x1b["


def _truncate_ansi(line: str, width: int) -> str:
    """Truncate visible text while preserving ANSI sequences and reset color."""
    if width <= 0:
        return ""

    output: list[str] = []
    visible = 0
    active_sgr = False
    index = 0
    truncated = False

    while index < len(line):
        if line.startswith(_ANSI_CSI_PREFIX, index):
            end = index + 2
            while end < len(line):
                char = line[end]
                end += 1
                if "\x40" <= char <= "\x7e":
                    break
            sequence = line[index:end]
            if visible >= width:
                truncated = True
                if sequence.endswith("m") and (
                    sequence == "\x1b[0m" or sequence == "\x1b[m"
                ):
                    if active_sgr:
                        output.append(sequence)
                    active_sgr = False
                index = end
                if not sequence.endswith("m"):
                    break
                continue
            output.append(sequence)
            if sequence.endswith("m"):
                if sequence == "\x1b[0m" or sequence == "\x1b[m":
                    active_sgr = False
                else:
                    active_sgr = True
            index = end
            continue

        if visible >= width:
            truncated = True
            break
        output.append(line[index])
        visible += 1
        index += 1

    if truncated and active_sgr:
        output.append("\x1b[0m")
    return "".join(output)


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
        if self._height > 1:
            self.stream.write(f"\x1b[{self._height - 1}A")
        self.stream.write("\r")
        width = self.size().columns
        for index, line in enumerate(lines):
            self.stream.write("\x1b[2K")
            self.stream.write(_truncate_ansi(line, width))
            if index + 1 < len(lines):
                self.stream.write("\n")
        self.stream.write("\x1b[J")
        self.stream.flush()
        self._height = len(lines)

    def size(self) -> os.terminal_size:
        return shutil.get_terminal_size()

    def close(self) -> None:
        if self._height > 0:
            self.stream.write("\r\n")
        self.stream.flush()

