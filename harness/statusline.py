"""Claude Code status line and rate-limit cache writer."""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO


def home_dir() -> Path:
    """Return the home directory used for user-level harness files."""
    return Path.home()


def _format_window(window: object, label: str) -> str | None:
    if not isinstance(window, dict):
        return None
    percentage = window.get("used_percentage")
    if percentage is None:
        return None
    reset = window.get("resets_at")
    when = ""
    if isinstance(reset, (int, float)):
        when = " until " + datetime.fromtimestamp(reset).strftime("%a %H:%M")
    return f"{label} {float(percentage):.0f}%{when}"


def render_statusline(
    data: object,
    *,
    cache_path: Path | None = None,
    saved_at: float | None = None,
) -> str:
    """Render one status line and cache any rate limits in *data*."""
    if not isinstance(data, dict):
        return ""

    model_data = data.get("model")
    model = model_data.get("display_name") if isinstance(model_data, dict) else ""
    parts = [str(model)] if model else []
    limits = data.get("rate_limits")
    if isinstance(limits, dict) and limits:
        target = cache_path or home_dir() / ".claude" / "usage-cache.json"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            payload: dict[str, Any] = {
                "saved_at": time.time() if saved_at is None else saved_at,
                "rate_limits": limits,
            }
            target.write_text(json.dumps(payload), encoding="utf-8")
        except OSError:
            pass
        for key, label in (("five_hour", "5h"), ("seven_day", "week")):
            text = _format_window(limits.get(key), label)
            if text:
                parts.append(text)
    return " | ".join(parts)


def main(*, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
    input_stream = stdin or sys.stdin
    output_stream = stdout or sys.stdout
    try:
        data = json.load(input_stream)
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        print("", file=output_stream)
        return 0
    print(render_statusline(data), file=output_stream)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
