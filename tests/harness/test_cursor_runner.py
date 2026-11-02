from __future__ import annotations

import json
from pathlib import Path

from harness.config import HarnessConfig
from harness.runner import CursorAgentRunner
from harness.usage import UsageRecorder
from tests.portable import make_python_script, yaml_quote


FIXTURE = Path(__file__).parent / "fixtures" / "cursor-stream-sample.jsonl"


def _config(tmp_path: Path, command: str = "agent", *, output_format: str | None = None) -> HarnessConfig:
    format_line = f"  output_format: {output_format}\n" if output_format else ""
    path = tmp_path / "harness.yaml"
    path.write_text(
        f"""execution: {{watchdog_poll_seconds: 0.01}}
worktree: {{}}
code_map: {{command: graphify}}
cursor:
  command: {yaml_quote(command)}
{format_line}  models: {{worker: test-model}}
verification: {{}}
paths: {{}}
context: {{}}
doctor: {{}}
""",
        encoding="utf-8",
    )
    return HarnessConfig.load(path)


def test_cursor_stream_fixture_extracts_answer_usage_and_activity(tmp_path: Path):
    runner = CursorAgentRunner(_config(tmp_path), tmp_path / "logs")
    raw = FIXTURE.read_text(encoding="utf-8")

    answer, diagnostics, usage, code_map_calls = runner._parse_output(raw, "graphify")

    assert answer == "DONE"
    assert diagnostics == ""
    assert usage == {
        "input_tokens": 7995,
        "cached_input_tokens": 15872,
        "output_tokens": 67,
    }
    assert code_map_calls == 0


def test_cursor_counts_completed_code_map_shell_call_once(tmp_path: Path):
    runner = CursorAgentRunner(_config(tmp_path), tmp_path / "logs")
    raw = FIXTURE.read_text(encoding="utf-8")
    event = {
        "type": "tool_call",
        "subtype": "completed",
        "tool_call": {"shellToolCall": {"args": {"command": "pwsh -c 'graphify explain thing'"}}},
    }

    _, _, _, code_map_calls = runner._parse_output(
        json.dumps(event) + "\n" + raw, "graphify"
    )

    assert code_map_calls == 1


def test_cursor_stream_invocation_keeps_raw_log_but_returns_only_final_answer(tmp_path: Path):
    stream = "\n".join(
        [
            json.dumps({"type": "thinking", "subtype": "completed", "text": "private"}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "draft"}]}}),
            json.dumps({
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": "VERDICT: FAIL\nFix the bug",
                "usage": {"inputTokens": 10, "outputTokens": 3, "cacheReadTokens": 4},
            }),
        ]
    )
    fake = make_python_script(
        tmp_path / "fake-cursor.py",
        f"import sys\nsys.stdout.write({stream!r})\n",
    )
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    runner = CursorAgentRunner(_config(tmp_path, fake), tmp_path / "logs", usage=usage)

    result = runner._invoke(
        task_id="REVIEW", phase="review", prompt="review", workspace=tmp_path,
        model_class="worker", timeout_minutes=1, log_name="review.log",
    )

    assert result.ok
    assert result.output == "VERDICT: FAIL\nFix the bug"
    assert "thinking" not in result.output
    assert '"type": "thinking"' in (tmp_path / "logs" / "review.log").read_text(encoding="utf-8")
    event = usage.events()[0]
    assert event["input_tokens"] == 10
    assert event["cached_input_tokens"] == 4
    assert event["output_tokens"] == 3
    assert event["code_map_calls"] == 0


def test_cursor_stream_quota_error_is_detected_without_exposing_events(tmp_path: Path):
    stream = json.dumps({
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "result": "quota exceeded: out of included usage",
        "usage": {"inputTokens": 1, "outputTokens": 1},
    })
    fake = make_python_script(
        tmp_path / "fake-quota.py",
        f"import sys\nsys.stdout.write({stream!r})\nraise SystemExit(1)\n",
    )
    runner = CursorAgentRunner(_config(tmp_path, fake), tmp_path / "logs")

    result = runner._invoke(
        task_id="QUOTA", phase="implement", prompt="x", workspace=tmp_path,
        model_class="worker", timeout_minutes=1, log_name="quota.log",
    )

    assert not result.ok
    assert result.quota_exhausted
    assert result.output == "quota exceeded: out of included usage"


def test_cursor_text_output_format_remains_raw(tmp_path: Path):
    raw_answer = '{"type":"result","result":"changed"}\n'
    fake = make_python_script(
        tmp_path / "fake-text.py",
        f"import sys\nsys.stdout.write({raw_answer!r})\n",
    )
    config = _config(tmp_path, fake, output_format="text")
    runner = CursorAgentRunner(config, tmp_path / "logs")

    result = runner._invoke(
        task_id="TEXT", phase="implement", prompt="x", workspace=tmp_path,
        model_class="worker", timeout_minutes=1, log_name="text.log",
    )

    assert result.ok
    assert result.output == raw_answer
    assert config.cursor.output_format == "text"
