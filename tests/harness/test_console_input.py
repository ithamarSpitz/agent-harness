from __future__ import annotations

from collections import deque

import pytest

from harness.console_input import KeyReader, LineEditor


@pytest.mark.parametrize(
    ("keys", "text", "cursor", "result"),
    [
        (["a", "b", "LEFT", "c", "ENTER"], "", 0, "acb"),
        (["a", "b", "BACKSPACE"], "a", 1, None),
        (["a", "b", "HOME", "x", "END", "y"], "xaby", 4, None),
        (["a", "b", "ESCAPE"], "", 0, None),
    ],
)
def test_line_editor_table(keys, text, cursor, result):
    editor = LineEditor()
    actual = None
    for key in keys:
        value = editor.feed(key)
        if value is not None:
            actual = value
    assert (editor.text, editor.cursor, actual) == (text, cursor, result)


def test_line_editor_history_and_draft():
    editor = LineEditor()
    for line in ("one", "two"):
        for char in line:
            editor.feed(char)
        assert editor.feed("ENTER") == line
    editor.feed("d")
    editor.feed("UP")
    assert editor.text == "two"
    editor.feed("UP")
    assert editor.text == "one"
    editor.feed("DOWN")
    editor.feed("DOWN")
    assert editor.text == "d"


def test_line_editor_keeps_last_fifty_commands():
    editor = LineEditor()
    for number in range(51):
        for char in str(number):
            editor.feed(char)
        editor.feed("ENTER")
    for _ in range(60):
        editor.feed("UP")
    assert editor.text == "1"


def test_line_editor_ctrl_c():
    with pytest.raises(KeyboardInterrupt):
        LineEditor().feed("\x03")


def test_windows_and_posix_streams_have_same_keys():
    windows_chars = deque(["a", "\xe0", "K", "\r", "\x08", "\x1b", "\x03"])
    windows = KeyReader(
        platform="win32",
        kbhit=lambda: bool(windows_chars),
        getwch=windows_chars.popleft,
    )

    posix_chars = deque(b"a\x1b[D\r\x7f\x1b\x03")
    posix = KeyReader(
        platform="linux",
        fileno=7,
        read=lambda _fd, _size: bytes([posix_chars.popleft()]),
        select=lambda read, _write, _error, _timeout: (read if posix_chars else [], [], []),
        tcgetattr=lambda _fd: [],
        tcsetattr=lambda *_args: None,
        setcbreak=lambda _fd: None,
    )
    assert windows.poll() == posix.poll() == [
        "a",
        "LEFT",
        "ENTER",
        "BACKSPACE",
        "ESCAPE",
        "CTRL_C",
    ]


def test_posix_decodes_utf8_character_split_across_polls():
    posix_chars = deque([b"\xc3"])
    reader = KeyReader(
        platform="linux",
        fileno=7,
        read=lambda _fd, _size: posix_chars.popleft(),
        select=lambda read, _write, _error, _timeout: (
            read if posix_chars else [],
            [],
            [],
        ),
        tcgetattr=lambda _fd: [],
        tcsetattr=lambda *_args: None,
        setcbreak=lambda _fd: None,
    )

    assert reader.poll() == []
    posix_chars.append(b"\xa9")
    assert reader.poll() == ["é"]


def test_posix_buffers_escape_sequence_split_across_polls():
    posix_chars = deque(b"\x1b")
    reader = KeyReader(
        platform="linux",
        fileno=7,
        read=lambda _fd, _size: bytes([posix_chars.popleft()]),
        select=lambda read, _write, _error, _timeout: (
            read if posix_chars else [],
            [],
            [],
        ),
        tcgetattr=lambda _fd: [],
        tcsetattr=lambda *_args: None,
        setcbreak=lambda _fd: None,
    )

    assert reader.poll() == []
    posix_chars.extend(b"[")
    assert reader.poll() == []
    posix_chars.extend(b"D")
    assert reader.poll() == ["LEFT"]


def test_posix_emits_standalone_escape_on_next_empty_poll():
    posix_chars = deque(b"\x1b")
    reader = KeyReader(
        platform="linux",
        fileno=7,
        read=lambda _fd, _size: bytes([posix_chars.popleft()]),
        select=lambda read, _write, _error, _timeout: (
            read if posix_chars else [],
            [],
            [],
        ),
        tcgetattr=lambda _fd: [],
        tcsetattr=lambda *_args: None,
        setcbreak=lambda _fd: None,
    )

    assert reader.poll() == []
    assert reader.poll() == ["ESCAPE"]


@pytest.mark.parametrize("exception", [None, ValueError, KeyboardInterrupt])
def test_posix_terminal_mode_is_always_restored(exception):
    calls = []
    reader = KeyReader(
        platform="linux",
        fileno=9,
        read=lambda _fd, _size: b"",
        select=lambda *_args: ([], [], []),
        tcgetattr=lambda fd: calls.append(("get", fd)) or ["original"],
        tcsetattr=lambda fd, when, mode: calls.append(("restore", fd, when, mode)),
        setcbreak=lambda fd: calls.append(("cbreak", fd)),
        restore_when=42,
    )
    if exception is None:
        with reader:
            pass
    else:
        with pytest.raises(exception):
            with reader:
                raise exception
    assert calls == [
        ("get", 9),
        ("cbreak", 9),
        ("restore", 9, 42, ["original"]),
    ]
