"""Non-blocking terminal input and a small command-line editor."""

from __future__ import annotations

import codecs
import os
import sys
from collections.abc import Callable
from importlib import import_module
from typing import Any


_CHAR_KEYS = {
    "\r": "ENTER",
    "\n": "ENTER",
    "\x08": "BACKSPACE",
    "\x7f": "BACKSPACE",
    "\x03": "CTRL_C",
    "\x1b": "ESCAPE",
}


class LineEditor:
    """Pure state machine for editing one line of terminal input."""

    def __init__(self, history_limit: int = 50) -> None:
        self.text = ""
        self.cursor = 0
        self._history: list[str] = []
        self._history_limit = history_limit
        self._history_index: int | None = None
        self._draft = ""

    def feed(self, key: str) -> str | None:
        """Apply *key*, returning a completed line only when Enter is pressed."""
        key = _CHAR_KEYS.get(key, key.upper() if len(key) > 1 else key)

        if key == "CTRL_C":
            raise KeyboardInterrupt
        if key == "ENTER":
            line = self.text
            if line:
                self._history.append(line)
                del self._history[: -self._history_limit]
            self._set_line("")
            self._leave_history()
            return line
        if key == "ESCAPE":
            self._set_line("")
            self._leave_history()
        elif key == "BACKSPACE":
            if self.cursor:
                self.text = self.text[: self.cursor - 1] + self.text[self.cursor :]
                self.cursor -= 1
        elif key == "LEFT":
            self.cursor = max(0, self.cursor - 1)
        elif key == "RIGHT":
            self.cursor = min(len(self.text), self.cursor + 1)
        elif key == "HOME":
            self.cursor = 0
        elif key == "END":
            self.cursor = len(self.text)
        elif key == "UP":
            self._history_up()
        elif key == "DOWN":
            self._history_down()
        elif len(key) == 1 and key.isprintable():
            self.text = self.text[: self.cursor] + key + self.text[self.cursor :]
            self.cursor += 1
        return None

    def _set_line(self, text: str) -> None:
        self.text = text
        self.cursor = len(text)

    def _leave_history(self) -> None:
        self._history_index = None
        self._draft = ""

    def _history_up(self) -> None:
        if not self._history:
            return
        if self._history_index is None:
            self._draft = self.text
            self._history_index = len(self._history) - 1
        elif self._history_index > 0:
            self._history_index -= 1
        self._set_line(self._history[self._history_index])

    def _history_down(self) -> None:
        if self._history_index is None:
            return
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            self._set_line(self._history[self._history_index])
        else:
            self._set_line(self._draft)
            self._history_index = None


class KeyReader:
    """Read all currently available terminal keys without blocking.

    OS-facing functions are keyword-injectable so callers can test either platform
    without touching a real terminal.
    """

    _WINDOWS_KEYS = {
        "H": "UP",
        "P": "DOWN",
        "K": "LEFT",
        "M": "RIGHT",
        "G": "HOME",
        "O": "END",
    }
    _POSIX_KEYS = {
        "\x1b[A": "UP",
        "\x1b[B": "DOWN",
        "\x1b[C": "RIGHT",
        "\x1b[D": "LEFT",
        "\x1b[H": "HOME",
        "\x1b[F": "END",
        "\x1b[1~": "HOME",
        "\x1b[4~": "END",
    }

    def __init__(
        self,
        *,
        platform: str | None = None,
        kbhit: Callable[[], bool] | None = None,
        getwch: Callable[[], str] | None = None,
        fileno: Callable[[], int] | int | None = None,
        read: Callable[[int, int], bytes | str] | None = None,
        select: Callable[..., Any] | None = None,
        tcgetattr: Callable[[int], Any] | None = None,
        tcsetattr: Callable[[int, int, Any], None] | None = None,
        setcbreak: Callable[[int], None] | None = None,
        restore_when: int | None = None,
    ) -> None:
        self._windows = (platform or sys.platform).startswith("win")
        self._fd_source = fileno
        self._fd: int | None = None
        self._saved_mode: Any = None
        self._entered = False
        self._posix_chars = ""
        self._posix_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        if self._windows:
            if kbhit is None or getwch is None:
                import msvcrt

                kbhit = kbhit or msvcrt.kbhit
                getwch = getwch or msvcrt.getwch
            self._kbhit = kbhit
            self._getwch = getwch
            return

        self._read = read or os.read
        self._select = select or import_module("select").select
        termios_module: Any = None
        if tcgetattr is None or tcsetattr is None:
            termios_module = import_module("termios")
        self._tcgetattr = tcgetattr or termios_module.tcgetattr
        self._tcsetattr = tcsetattr or termios_module.tcsetattr
        self._setcbreak = setcbreak or import_module("tty").setcbreak
        self._restore_when = restore_when

    def __enter__(self) -> KeyReader:
        if not self._windows and not self._entered:
            source = sys.stdin.fileno if self._fd_source is None else self._fd_source
            self._fd = source() if callable(source) else source
            if self._restore_when is None:
                self._restore_when = import_module("termios").TCSADRAIN
            self._saved_mode = self._tcgetattr(self._fd)
            try:
                self._setcbreak(self._fd)
            except BaseException:
                self._tcsetattr(self._fd, self._restore_when, self._saved_mode)
                self._saved_mode = None
                raise
        self._entered = True
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        if (
            not self._windows
            and self._saved_mode is not None
            and self._fd is not None
            and self._restore_when is not None
        ):
            try:
                self._tcsetattr(self._fd, self._restore_when, self._saved_mode)
            finally:
                self._saved_mode = None
                self._entered = False

    def poll(self) -> list[str]:
        """Return every complete key that is immediately available."""
        return self._poll_windows() if self._windows else self._poll_posix()

    def _poll_windows(self) -> list[str]:
        keys: list[str] = []
        while self._kbhit():
            char = self._getwch()
            if char in {"\x00", "\xe0"}:
                char = self._getwch()
                mapped = self._WINDOWS_KEYS.get(char)
                if mapped is not None:
                    keys.append(mapped)
            else:
                keys.append(_CHAR_KEYS.get(char, char))
        return keys

    def _poll_posix(self) -> list[str]:
        if self._fd is None:
            source = sys.stdin.fileno if self._fd_source is None else self._fd_source
            self._fd = source() if callable(source) else source
        read_any = False
        while self._select([self._fd], [], [], 0)[0]:
            value = self._read(self._fd, 1)
            if not value:
                break
            read_any = True
            if isinstance(value, bytes):
                self._posix_chars += self._posix_decoder.decode(value, final=False)
            else:
                self._posix_chars += value

        keys, pending = self._decode_posix(self._posix_chars)
        if pending == "\x1b" and not read_any:
            keys.append("ESCAPE")
            pending = ""
        self._posix_chars = pending
        return keys

    @classmethod
    def _decode_posix(cls, chars: str) -> tuple[list[str], str]:
        keys: list[str] = []
        index = 0
        sequences = sorted(cls._POSIX_KEYS, key=len, reverse=True)
        while index < len(chars):
            match = next((seq for seq in sequences if chars.startswith(seq, index)), None)
            if match is not None:
                keys.append(cls._POSIX_KEYS[match])
                index += len(match)
                continue

            remainder = chars[index:]
            if any(sequence.startswith(remainder) for sequence in sequences):
                return keys, remainder

            char = chars[index]
            keys.append(_CHAR_KEYS.get(char, char))
            index += 1
        return keys, ""
