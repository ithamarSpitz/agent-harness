from __future__ import annotations

import io
import json
from datetime import datetime

from harness.statusline import main, render_statusline


def test_statusline_renders_limits_and_writes_cache(tmp_path):
    cache = tmp_path / ".claude" / "usage-cache.json"
    five_hour_reset = 1_800_000_000
    week_reset = 1_800_600_000
    data = {
        "model": {"display_name": "Claude Opus"},
        "rate_limits": {
            "five_hour": {"used_percentage": 12.4, "resets_at": five_hour_reset},
            "seven_day": {"used_percentage": 56.7, "resets_at": week_reset},
        },
    }

    line = render_statusline(data, cache_path=cache, saved_at=1234.5)

    five_hour_text = datetime.fromtimestamp(five_hour_reset).strftime("%a %H:%M")
    week_text = datetime.fromtimestamp(week_reset).strftime("%a %H:%M")
    assert line == f"Claude Opus | 5h 12% until {five_hour_text} | week 57% until {week_text}"
    assert json.loads(cache.read_text(encoding="utf-8")) == {
        "saved_at": 1234.5,
        "rate_limits": data["rate_limits"],
    }


def test_statusline_without_rate_limits_prints_only_model_and_writes_nothing(tmp_path):
    cache = tmp_path / "usage-cache.json"

    line = render_statusline({"model": {"display_name": "Claude Sonnet"}}, cache_path=cache)

    assert line == "Claude Sonnet"
    assert not cache.exists()


def test_statusline_main_accepts_sample_stdin(monkeypatch, tmp_path):
    cache = tmp_path / "usage-cache.json"
    monkeypatch.setattr("harness.statusline.home_dir", lambda: tmp_path.parent)
    stdin = io.StringIO(json.dumps({"model": {"display_name": "Claude"}}))
    stdout = io.StringIO()

    assert main(stdin=stdin, stdout=stdout) == 0
    assert stdout.getvalue() == "Claude\n"
    assert not cache.exists()


def test_statusline_main_handles_invalid_json():
    stdout = io.StringIO()

    assert main(stdin=io.StringIO("not json"), stdout=stdout) == 0
    assert stdout.getvalue() == "\n"
