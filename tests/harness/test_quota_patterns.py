from __future__ import annotations

import json
from pathlib import Path

from harness.config import HarnessConfig, QuotaConfig
from harness.runner import CursorAgentRunner
from tests.portable import make_python_script, yaml_quote

CURSOR_OUT_OF_USAGE = (
    "ActionRequiredError: Increase limits for faster responses "
    "You're out of usage. Switch to Auto, or ask your admin to "
    "increase your limit to continue."
)


def _config(tmp_path: Path, command: str = "agent") -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(
        f"""execution: {{watchdog_poll_seconds: 0.01}}
worktree: {{}}
code_map: {{command: graphify}}
cursor:
  command: {yaml_quote(command)}
  models: {{worker: test-model}}
verification: {{}}
paths: {{}}
context: {{}}
doctor: {{}}
""",
        encoding="utf-8",
    )
    return HarnessConfig.load(path)


def test_default_exhaustion_patterns_include_cursor_out_of_usage():
    patterns = QuotaConfig().exhaustion_patterns
    assert "you're out of usage" in patterns
    assert "you’re out of usage" in patterns
    assert "increase limits for faster responses" in patterns
    # Existing patterns must remain.
    assert "usage limit reached" in patterns
    assert "quota exceeded" in patterns
    assert "no usage remaining" in patterns


def test_cursor_out_of_usage_failed_call_is_quota(tmp_path: Path):
    stream = json.dumps({
        "type": "result",
        "subtype": "error",
        "is_error": True,
        "result": CURSOR_OUT_OF_USAGE,
    })
    fake = make_python_script(
        tmp_path / "fake-out-of-usage.py",
        f"import sys\nsys.stdout.write({stream!r})\nraise SystemExit(1)\n",
    )
    runner = CursorAgentRunner(_config(tmp_path, str(fake)), tmp_path / "logs")

    result = runner._invoke(
        task_id="QUOTA", phase="implement", prompt="x", workspace=tmp_path,
        model_class="worker", timeout_minutes=1, log_name="out-of-usage.log",
    )

    assert not result.ok
    assert result.quota_exhausted
    assert result.error == "Cursor usage quota exhausted"


def test_cursor_out_of_usage_quoted_in_success_is_not_quota(tmp_path: Path):
    stream = "\n".join([
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}),
        json.dumps({
            "type": "result",
            "subtype": "success",
            "result": f"Docs say: {CURSOR_OUT_OF_USAGE}",
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }),
    ])
    fake = make_python_script(
        tmp_path / "fake-quote-success.py",
        f"import sys\nsys.stdout.write({stream!r})\n",
    )
    runner = CursorAgentRunner(_config(tmp_path, str(fake)), tmp_path / "logs")

    result = runner._invoke(
        task_id="OK", phase="implement", prompt="x", workspace=tmp_path,
        model_class="worker", timeout_minutes=1, log_name="quote-success.log",
    )

    assert result.ok
    assert not result.quota_exhausted
