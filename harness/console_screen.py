"""Compose board, command output and prompt into one fixed-size screen."""

from __future__ import annotations


PROMPT_PREFIX = "harness> "


def _truncate(line: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(line) <= width:
        return line
    return line[:width]


def _prompt_line(prompt_text: str, cursor: int, width: int) -> str:
    """Return a width-limited prompt slice that keeps *cursor* visible."""
    if width <= 0:
        return ""
    cursor = max(0, min(len(prompt_text), cursor))
    if len(prompt_text) <= width:
        return prompt_text
    # Keep the cursor on-screen, preferring the right edge of the window.
    start = max(0, cursor - width + 1)
    if start + width > len(prompt_text):
        start = len(prompt_text) - width
    return prompt_text[start : start + width]


def compose(
    board_lines: list[str],
    output_lines: list[str],
    prompt_text: str,
    cursor: int,
    width: int,
    height: int,
) -> list[str]:
    """Lay out board, output and prompt into exactly *height* lines.

    Board comes first, then as much of *output_lines* as fits (from the start),
    then blank padding so the prompt stays on the last row.
    """
    if height <= 0:
        return []

    prompt = _prompt_line(prompt_text, cursor, width)
    if height == 1:
        return [prompt]

    body_height = height - 1
    board = [_truncate(line, width) for line in board_lines]
    if len(board) >= body_height:
        return board[:body_height] + [prompt]

    out_budget = body_height - len(board)
    output = [_truncate(line, width) for line in output_lines[:out_budget]]
    pad = body_height - len(board) - len(output)
    return board + output + ([""] * pad) + [prompt]
