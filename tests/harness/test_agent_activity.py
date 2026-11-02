from __future__ import annotations

import json

from harness.codex_runner import CodexAgentRunner


def test_codex_counts_wrapped_code_map_command():
    raw = "\n".join(
        [
            json.dumps({
                "type": "item.completed",
                "item": {
                    "type": "command_execution",
                    "command": "cmd.exe /d /s /c \"graphify path harness.runner CursorAgentRunner\"",
                },
            }),
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "DONE"},
            }),
        ]
    )

    answer, diagnostics, _, code_map_calls = CodexAgentRunner._parse_events(raw, "graphify")

    assert answer == "DONE"
    assert diagnostics == ""
    assert code_map_calls == 1


def test_code_map_command_must_appear_as_a_word():
    raw = json.dumps({
        "type": "item.completed",
        "item": {"type": "command_execution", "command": "graphify-helper path x y"},
    })

    _, _, _, code_map_calls = CodexAgentRunner._parse_events(raw, "graphify")

    assert code_map_calls == 0
