from __future__ import annotations

import json
from dataclasses import asdict

import pytest

from harness.kiro_stream import KiroStream, classify_kiro, parse_kiro_stream


SUCCESS_STREAM = """\
{"type":"runStarted","data":{"payloadSchema":"acp","acpProtocolVersion":1,"engine":"v2"}}
{"type":"metadata","data":{"sessionId":"888118b0","contextUsagePercentage":3.55,"reasoning":{"support":"unavailable","effortLevels":[]}}}
{"type":"sessionUpdate","data":{"sessionId":"888118b0","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"PONG"}}}}
{"type":"sessionUpdate","data":{"sessionId":"888118b0","update":{"sessionUpdate":"tool_call","toolCallId":"t1","title":"Running: git status --short","kind":"execute","rawInput":{"command":"git status --short"}}}}
[denied] tool permission approval is not supported in non-interactive mode. Use --trust-all-tools to auto-approve.
{"type":"sessionUpdate","data":{"sessionId":"888118b0","update":{"sessionUpdate":"tool_call_update","toolCallId":"t1","kind":"execute","status":"failed","title":"shell"}}}
{"type":"metadata","data":{"sessionId":"888118b0","contextUsagePercentage":6.17,"meteringUsage":[{"value":0.0158,"unit":"credit","unitPlural":"credits"}],"turnDurationMs":1381}}
{"type":"runFinished","data":{"sessionId":"888118b0","status":"success","stopReason":"end_turn","finalText":"PONG","finalTextTruncated":false}}
"""

UNKNOWN_MODEL_STREAM = """\
{"type":"runStarted","data":{"payloadSchema":"acp","acpProtocolVersion":1,"engine":"v2"}}
{"type":"runError","data":{"sessionId":"e409e0f3","stage":"prompt","message":"Internal error (code -32603): Encountered an error in the response stream: The model 'no-such-model' is not available. Please use '/model' to select a different model and try again. (request_id: 381e63b3)"}}
"""


def test_parse_success_sample() -> None:
    stream = parse_kiro_stream(SUCCESS_STREAM)

    assert stream.final_text == "PONG"
    assert stream.status == "success"
    assert stream.stop_reason == "end_turn"
    assert stream.error is None
    assert stream.credits == pytest.approx(0.0158)
    assert stream.denied_tools == 1
    assert stream.finished is True
    assert not any(asdict(classify_kiro(stream, 0, SUCCESS_STREAM)).values())


def test_parse_and_classify_unknown_model_sample() -> None:
    stream = parse_kiro_stream(UNKNOWN_MODEL_STREAM)
    flags = classify_kiro(stream, 1, UNKNOWN_MODEL_STREAM)

    assert stream.error is not None
    assert "is not available" in stream.error
    assert stream.finished is False
    assert asdict(flags) == {
        "quota": False,
        "auth": False,
        "model_unavailable": True,
        "capacity": False,
        "transient": False,
        "config": False,
    }


@pytest.mark.parametrize("final_text", [None, "partial final text"])
def test_truncated_or_missing_final_text_uses_joined_chunks(
    final_text: str | None,
) -> None:
    events = [
        {
            "type": "sessionUpdate",
            "data": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "part one "},
                }
            },
        },
        {
            "type": "sessionUpdate",
            "data": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"text": "part two"},
                }
            },
        },
        {
            "type": "runFinished",
            "data": {"status": "success", "finalTextTruncated": True},
        },
    ]
    if final_text is not None:
        events[-1]["data"]["finalText"] = final_text

    stream = parse_kiro_stream("\n".join(json.dumps(event) for event in events))

    assert stream.final_text == "part one part two"


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("OUT OF CREDITS", "quota"),
        ("Please run kiro-cli login", "auth"),
        ("The model 'gone' is not available", "model_unavailable"),
        ("Unexpected argument --bad", "config"),
        ("Too many requests", "capacity"),
        ("Service unavailable", "transient"),
    ],
)
def test_classifies_each_fault_category(message: str, expected: str) -> None:
    stream = _failed_stream(error=message)

    flags = asdict(classify_kiro(stream, 1, ""))

    assert flags[expected] is True
    assert sum(flags.values()) == 1


def test_classification_uses_raw_output() -> None:
    flags = classify_kiro(_failed_stream(), 1, "NETWORK failure")

    assert flags.transient is True


def test_quota_has_precedence_over_capacity() -> None:
    flags = asdict(classify_kiro(_failed_stream(error="quota hit: 429"), 1, ""))

    assert flags["quota"] is True
    assert sum(flags.values()) == 1


def test_unmatched_failure_has_no_flags() -> None:
    flags = classify_kiro(_failed_stream(error="task failed"), 1, "")

    assert not any(asdict(flags).values())


@pytest.mark.parametrize("text", ["", "garbage\n{not json}\n42\nnull"])
def test_empty_and_garbage_input_do_not_raise(text: str) -> None:
    stream = parse_kiro_stream(text)

    assert stream.final_text == ""
    assert stream.finished is False


def test_parser_sums_credit_entries_and_last_error_wins() -> None:
    text = "\n".join(
        (
            '{"type":"metadata","data":{"meteringUsage":'
            '[{"value":0.25,"unit":"credit"},{"value":10,"unit":"tokens"}]}}',
            '{"type":"metadata","data":{"meteringUsage":'
            '[{"value":1,"unit":"credit"}]}}',
            '{"type":"runError","data":{"message":"first"}}',
            '{"type":"runError","data":{"message":"last"}}',
        )
    )

    stream = parse_kiro_stream(text)

    assert stream.credits == pytest.approx(1.25)
    assert stream.error == "last"


def _failed_stream(*, error: str | None = None) -> KiroStream:
    return KiroStream(
        final_text="",
        status=None,
        stop_reason=None,
        error=error,
        credits=0.0,
        denied_tools=0,
        finished=False,
    )
