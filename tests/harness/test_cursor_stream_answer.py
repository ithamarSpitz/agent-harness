from __future__ import annotations

import json
from pathlib import Path

from harness.runner import CursorAgentRunner
from tests.harness.test_cursor_runner import _config

FIXTURE = Path(__file__).parent / "fixtures" / "cursor-review-stream.jsonl"


def test_cursor_review_stream_fixture_keeps_verdict_on_own_line(tmp_path: Path):
    runner = CursorAgentRunner(_config(tmp_path), tmp_path / "logs")
    raw = FIXTURE.read_text(encoding="utf-8")

    answer, diagnostics, usage, code_map_calls, reported_error = runner._parse_output(
        raw, "graphify"
    )

    assert "VERDICT: PASS" in answer.splitlines()
    assert any(line.strip() == "VERDICT: PASS" for line in answer.splitlines())
    assert diagnostics == ""
    assert usage == {
        "input_tokens": 1200,
        "cached_input_tokens": 800,
        "output_tokens": 45,
    }
    assert code_map_calls == 0
    assert not reported_error


def test_cursor_stream_keeps_result_when_it_differs_from_joined_messages(tmp_path: Path):
    runner = CursorAgentRunner(_config(tmp_path), tmp_path / "logs")
    stream = "\n".join(
        [
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "draft notes"}]},
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {"content": [{"type": "text", "text": "VERDICT: PASS"}]},
                }
            ),
            json.dumps(
                {
                    "type": "result",
                    "subtype": "success",
                    "is_error": False,
                    "result": "VERDICT: FAIL\nNeeds a fix",
                }
            ),
        ]
    )

    answer, _, _, _, _ = runner._parse_output(stream, "graphify")

    assert answer == "VERDICT: FAIL\nNeeds a fix"
