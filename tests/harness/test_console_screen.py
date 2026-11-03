from __future__ import annotations

import pytest

from harness.console_screen import PROMPT_PREFIX, compose


def _board(n: int, prefix: str = "B") -> list[str]:
    return [f"{prefix}{i}" for i in range(n)]


def _output(n: int) -> list[str]:
    return [f"out{i}" for i in range(n)]


@pytest.mark.parametrize("width", [60, 100])
@pytest.mark.parametrize("height", [15, 30])
def test_compose_exact_fixed_inputs(width: int, height: int) -> None:
    board = _board(5)
    output = _output(20)
    prompt = f"{PROMPT_PREFIX}task S1-T1"
    cursor = len(prompt)
    lines = compose(board, output, prompt, cursor, width, height)

    assert len(lines) == height
    assert lines[:5] == board
    body = height - 1
    out_budget = body - 5
    shown = output[:out_budget]
    assert lines[5 : 5 + len(shown)] == shown
    pad = body - 5 - len(shown)
    assert lines[5 + len(shown) : -1] == [""] * pad
    assert lines[-1] == prompt
    assert all(len(line) <= width for line in lines)


def test_compose_truncates_board_when_taller_than_body() -> None:
    board = _board(20)
    lines = compose(board, ["ignored"], f"{PROMPT_PREFIX}", 0, 40, 10)
    assert len(lines) == 10
    assert lines[:9] == board[:9]
    assert lines[-1] == PROMPT_PREFIX


def test_compose_truncates_wide_lines() -> None:
    lines = compose(
        ["x" * 80],
        ["y" * 80],
        "p" * 80,
        80,
        width=60,
        height=4,
    )
    assert lines == ["x" * 60, "y" * 60, "", "p" * 60]


def test_compose_keeps_cursor_visible_on_long_prompt() -> None:
    prompt = PROMPT_PREFIX + ("a" * 100)
    cursor = len(prompt)
    line = compose([], [], prompt, cursor, width=20, height=1)[0]
    assert len(line) == 20
    assert line == prompt[-20:]


def test_compose_empty_height() -> None:
    assert compose(["a"], ["b"], "p", 0, 80, 0) == []
