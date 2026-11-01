from __future__ import annotations

import io
import json
import sqlite3
import urllib.error
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness.cli import main
from harness.limits import (
    CURSOR_TOKEN_KEY,
    CURSOR_USAGE_URL,
    LimitsPaths,
    find_newest_rollout_file,
    read_last_codex_rate_limits,
    render_limits,
    run_limits,
)


FAKE_CURSOR_TOKEN = "cursor-secret-test-token-do-not-print"


def write_rollout(path: Path, payload: dict, *, timestamp: int, append: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"type": "event_msg", "timestamp": timestamp, "payload": payload}) + "\n"
    if append:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        return
    path.write_text(line, encoding="utf-8")


def write_cursor_db(path: Path, token: str = FAKE_CURSOR_TOKEN) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO ItemTable (key, value) VALUES (?, ?)",
            (CURSOR_TOKEN_KEY, token),
        )
        conn.commit()
    finally:
        conn.close()


def make_paths(tmp_path: Path) -> LimitsPaths:
    codex_home = tmp_path / "codex"
    claude_home = tmp_path / "claude"
    claude_home.mkdir(parents=True, exist_ok=True)
    return LimitsPaths(
        codex_home=codex_home,
        claude_usage_cache=claude_home / "usage-cache.json",
        cursor_state_db=tmp_path / "cursor" / "state.vscdb",
        claude_home=claude_home,
    )


def fixed_now() -> datetime:
    return datetime(2026, 11, 1, 12, 0, tzinfo=timezone(timedelta(hours=2)))


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _response(body: bytes) -> _Response:
    return _Response(body)


def _fake_cursor_opener(_paths: LimitsPaths):
    def opener(request, timeout=20):
        end_ms = int((fixed_now() + timedelta(days=3)).timestamp() * 1000)
        body = {
            "billingCycleEnd": end_ms,
            "planUsage": {"autoPercentUsed": 5, "apiPercentUsed": 0},
        }
        return _response(json.dumps(body).encode("utf-8"))

    return opener


def _unavailable_cursor_opener():
    def opener(request, timeout=20):
        raise urllib.error.HTTPError(
            CURSOR_USAGE_URL,
            503,
            "unavailable",
            hdrs=None,
            fp=None,
        )

    return opener


def _http_401_opener():
    def opener(request, timeout=20):
        raise urllib.error.HTTPError(
            CURSOR_USAGE_URL,
            401,
            "unauthorized",
            hdrs=None,
            fp=None,
        )

    return opener


def test_codex_uses_newest_rollout_and_last_rate_limits_line(tmp_path: Path):
    paths = make_paths(tmp_path)
    older = paths.codex_home / "sessions" / "a" / "rollout-old.jsonl"
    newer = paths.codex_home / "sessions" / "b" / "rollout-new.jsonl"
    write_rollout(
        older,
        {
            "type": "token_count",
            "plan_type": "free",
            "rate_limits": {
                "primary": {"used_percent": 99, "resets_at": 1_762_000_000},
            },
        },
        timestamp=1_761_900_000,
    )
    write_rollout(
        newer,
        {
            "type": "token_count",
            "plan_type": "stale",
            "rate_limits": {
                "primary": {
                    "used_percent": 99,
                    "window_minutes": 300,
                    "resets_at": int(fixed_now().timestamp()) + 3600,
                },
            },
        },
        timestamp=int(fixed_now().timestamp()) - 60,
    )
    with newer.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "other", "payload": {"type": "token_count"}}) + "\n")
        handle.write(json.dumps({"type": "event_msg", "payload": {"type": "token_count"}}) + "\n")
    write_rollout(
        newer,
        {
            "type": "token_count",
            "plan_type": "plus",
            "credits": {"has_credits": True, "balance": 12.5, "unlimited": False},
            "rate_limits": {
                "primary": {
                    "used_percent": 40,
                    "window_minutes": 300,
                    "resets_at": int(fixed_now().timestamp()) + 7200,
                },
                "secondary": {
                    "used_percent": 10,
                    "window_minutes": 10080,
                    "resets_at": int(fixed_now().timestamp()) + 86400,
                },
            },
        },
        timestamp=int(fixed_now().timestamp()),
        append=True,
    )
    import os

    os.utime(older, (1_700_000_000, 1_700_000_000))
    os.utime(newer, (1_800_000_000, 1_800_000_000))

    assert find_newest_rollout_file(paths.codex_home) == newer
    payload, _ = read_last_codex_rate_limits(newer)
    assert payload["plan_type"] == "plus"
    assert payload["rate_limits"]["primary"]["used_percent"] == 40

    text = render_limits(paths, now=fixed_now(), opener=_fake_cursor_opener(paths))
    assert "Codex (plus)" in text
    assert "Codex (stale)" not in text
    assert "5-hour" in text
    assert "weekly" in text
    assert "40% used" in text
    assert "99% used" not in text
    assert "credits  12.5" in text
    assert "(last Codex call)" in text


def test_codex_past_reset_shows_zero_percent(tmp_path: Path):
    paths = make_paths(tmp_path)
    rollout = paths.codex_home / "sessions" / "x" / "rollout-1.jsonl"
    past = int(fixed_now().timestamp()) - 3600
    write_rollout(
        rollout,
        {
            "type": "token_count",
            "plan_type": "plus",
            "rate_limits": {
                "primary": {"used_percent": 80, "resets_at": past},
            },
        },
        timestamp=past,
    )
    text = render_limits(paths, now=fixed_now(), opener=_unavailable_cursor_opener())
    assert "0% (reset at" in text


def test_codex_missing_sessions_is_unavailable(tmp_path: Path):
    paths = make_paths(tmp_path)
    text = render_limits(paths, now=fixed_now(), opener=_unavailable_cursor_opener())
    assert "Codex: unavailable (no session logs)" in text


def test_cursor_sends_expected_request_and_formats_usage(tmp_path: Path):
    paths = make_paths(tmp_path)
    write_cursor_db(paths.cursor_state_db)
    captured: dict[str, object] = {}

    def opener(request, timeout=20):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["data"] = request.data
        captured["headers"] = {k: v for k, v in request.header_items()}
        end_ms = int((fixed_now() + timedelta(days=10)).timestamp() * 1000)
        body = {
            "billingCycleEnd": str(end_ms),
            "planUsage": {"autoPercentUsed": 19.4, "apiPercentUsed": 0.2},
            "spendLimitUsage": {},
        }
        return _response(json.dumps(body).encode("utf-8"))

    text = render_limits(paths, now=fixed_now(), opener=opener)
    assert captured["url"] == CURSOR_USAGE_URL
    assert captured["method"] == "POST"
    assert captured["data"] == b"{}"
    headers = {k.lower(): v for k, v in captured["headers"].items()}  # type: ignore[union-attr]
    assert headers["authorization"] == f"Bearer {FAKE_CURSOR_TOKEN}"
    assert headers["content-type"] == "application/json"
    assert headers["connect-protocol-version"] == "1"
    assert "Cursor (monthly)" in text
    assert "19% used" in text
    assert "0% used" in text
    assert "On-demand spending              disabled" in text
    assert "days left" in text
    assert FAKE_CURSOR_TOKEN not in text


def test_cursor_http_401_and_missing_db_are_unavailable(tmp_path: Path):
    paths = make_paths(tmp_path)
    paths.cursor_state_db.parent.mkdir(parents=True, exist_ok=True)
    text_missing = render_limits(paths, now=fixed_now(), opener=_fake_cursor_opener(paths))
    assert "Cursor: unavailable (state database missing)" in text_missing

    write_cursor_db(paths.cursor_state_db)
    text = render_limits(paths, now=fixed_now(), opener=_http_401_opener())
    assert "Cursor: unavailable (HTTP 401)" in text


def test_cursor_token_never_leaks_to_json_or_errors(tmp_path: Path):
    paths = make_paths(tmp_path)
    write_cursor_db(paths.cursor_state_db)

    def bad_opener(request, timeout=20):
        raise RuntimeError(f"failed with token {FAKE_CURSOR_TOKEN}")

    buf = io.StringIO()
    with redirect_stdout(buf):
        run_limits(as_json=True, paths=paths, opener=bad_opener)
    out = buf.getvalue()
    assert FAKE_CURSOR_TOKEN not in out
    parsed = json.loads(out)
    assert "error" in parsed["cursor"]


def test_claude_formats_windows_and_missing_file_message(tmp_path: Path):
    paths = make_paths(tmp_path)
    now = fixed_now()
    paths.claude_usage_cache.write_text(
        json.dumps(
            {
                "saved_at": int(now.timestamp()) - 120,
                "rate_limits": {
                    "five_hour": {
                        "used_percentage": 33,
                        "resets_at": int(now.timestamp()) + 3600,
                    },
                    "seven_day": {
                        "used_percentage": 5,
                        "resets_at": int(now.timestamp()) + 86400,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    text = render_limits(paths, now=now, opener=_unavailable_cursor_opener())
    assert "Codex: unavailable" in text
    assert "Claude" in text
    assert "5-hour" in text
    assert "weekly" in text
    assert "(last Claude Code reply)" in text

    paths.claude_usage_cache.unlink()
    text_missing = render_limits(paths, now=now, opener=_unavailable_cursor_opener())
    assert "Claude: no data yet (Claude Code saves it on its next reply)" in text_missing


def test_claude_skips_missing_window(tmp_path: Path):
    paths = make_paths(tmp_path)
    now = fixed_now()
    paths.claude_usage_cache.write_text(
        json.dumps(
            {
                "saved_at": int(now.timestamp()),
                "rate_limits": {
                    "five_hour": {
                        "used_percentage": 12,
                        "resets_at": int(now.timestamp()) + 1000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    text = render_limits(paths, now=now, opener=_unavailable_cursor_opener())
    assert "5-hour" in text
    assert "weekly" not in text.split("Claude")[-1]


def test_codex_malformed_rate_limits_does_not_abort_command(tmp_path: Path):
    paths = make_paths(tmp_path)
    now = fixed_now()
    rollout = paths.codex_home / "sessions" / "x" / "rollout-1.jsonl"
    rollout.parent.mkdir(parents=True, exist_ok=True)
    rollout.write_text(
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": int(now.timestamp()),
                "payload": {"type": "token_count", "rate_limits": None},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "timestamp": int(now.timestamp()),
                "payload": {"type": "token_count", "rate_limits": ["not", "a", "dict"]},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "event_msg",
                "timestamp": int(now.timestamp()),
                "payload": {
                    "type": "token_count",
                    "rate_limits": {"primary": ["also", "invalid"]},
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    paths.claude_usage_cache.write_text(
        json.dumps(
            {
                "saved_at": int(now.timestamp()),
                "rate_limits": {
                    "five_hour": {
                        "used_percentage": 1,
                        "resets_at": int(now.timestamp()) + 60,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    write_cursor_db(paths.cursor_state_db)
    text = render_limits(paths, now=now, opener=_fake_cursor_opener(paths))
    assert "Codex: unavailable" in text
    assert "Cursor (monthly)" in text
    assert "Claude" in text
    assert "5-hour" in text
    assert "(last Claude Code reply)" in text


def test_one_failing_service_does_not_block_others(tmp_path: Path):
    paths = make_paths(tmp_path)
    now = fixed_now()
    paths.claude_usage_cache.write_text(
        json.dumps(
            {
                "saved_at": int(now.timestamp()),
                "rate_limits": {
                    "five_hour": {
                        "used_percentage": 1,
                        "resets_at": int(now.timestamp()) + 60,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    write_cursor_db(paths.cursor_state_db)
    text = render_limits(paths, now=now, opener=_fake_cursor_opener(paths))
    assert "Codex: unavailable" in text
    assert "Cursor (monthly)" in text
    assert "Claude" in text


def test_limits_cli_runs_without_harness_yaml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude"))
    db = tmp_path / "cursor" / "state.vscdb"
    write_cursor_db(db)
    monkeypatch.setattr(
        "harness.limits.default_cursor_state_db",
        lambda: db,
    )
    http_calls: list[object] = []

    def opener(request, timeout=20):
        http_calls.append((request, timeout))
        end_ms = int((fixed_now() + timedelta(days=3)).timestamp() * 1000)
        body = {
            "billingCycleEnd": end_ms,
            "planUsage": {"autoPercentUsed": 5, "apiPercentUsed": 0},
        }
        return _response(json.dumps(body).encode("utf-8"))

    monkeypatch.setattr("harness.limits.urllib.request.urlopen", opener)
    empty = tmp_path / "empty"
    empty.mkdir()
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["--root", str(empty), "limits"])
    assert code == 0
    out = buf.getvalue()
    assert http_calls
    assert http_calls[0][1] == 20
    assert "Codex: unavailable" in out
    assert "Cursor (monthly)" in out
    assert "Claude: no data yet" in out
    assert FAKE_CURSOR_TOKEN not in out
