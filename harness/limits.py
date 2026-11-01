from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

CURSOR_USAGE_URL = (
    "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
)
CURSOR_TOKEN_KEY = "cursorAuth/accessToken"


@dataclass(slots=True)
class LimitsPaths:
    codex_home: Path
    claude_usage_cache: Path
    cursor_state_db: Path
    claude_home: Path | None = None

    @classmethod
    def from_env(cls) -> LimitsPaths:
        codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
        claude_home = Path(os.environ.get("CLAUDE_HOME", Path.home() / ".claude")).expanduser()
        return cls(
            codex_home=codex_home,
            claude_usage_cache=claude_home / "usage-cache.json",
            cursor_state_db=default_cursor_state_db(),
            claude_home=claude_home,
        )


def default_cursor_state_db() -> Path:
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise OSError("APPDATA is not set")
        return Path(appdata) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    home = Path.home()
    if sys.platform == "darwin":
        return (
            home
            / "Library"
            / "Application Support"
            / "Cursor"
            / "User"
            / "globalStorage"
            / "state.vscdb"
        )
    return home / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


UrlOpener = Callable[..., Any]


def format_local_day_time(when: datetime) -> str:
    local = when.astimezone()
    text = local.strftime("%a %b %d, %I:%M %p")
    return text.replace(" 0", " ", 1) if " 0" in text[:6] else text


def format_local_date(when: datetime) -> str:
    local = when.astimezone()
    text = local.strftime("%a %b %d, %Y")
    return text.replace(" 0", " ", 1) if "  0" in text else text


def _parse_timestamp(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1_000_000_000_000:
            ts /= 1000.0
        return ts
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _parse_timestamp(int(text))
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _line_timestamp(obj: dict[str, Any], fallback: float | None) -> float | None:
    for key in ("timestamp", "ts", "time", "created_at"):
        parsed = _parse_timestamp(obj.get(key))
        if parsed is not None:
            return parsed
    payload = obj.get("payload")
    if isinstance(payload, dict):
        for key in ("timestamp", "ts", "time"):
            parsed = _parse_timestamp(payload.get(key))
            if parsed is not None:
                return parsed
    return fallback


def _format_window_line(
    label: str,
    used_percent: float,
    resets_at: int,
    *,
    now: datetime,
) -> str:
    reset_dt = datetime.fromtimestamp(resets_at, tz=timezone.utc).astimezone()
    reset_label = format_local_day_time(reset_dt)
    if resets_at <= int(now.timestamp()):
        return f"  {label:<8} 0% (reset at {reset_label})"
    hours = int((reset_dt - now).total_seconds()) // 3600
    minutes = (int((reset_dt - now).total_seconds()) % 3600) // 60
    pct = int(round(used_percent))
    return f"  {label:<8} {pct}% used, resets {reset_label} (in {hours}h {minutes}m)"


def find_newest_rollout_file(codex_home: Path) -> Path | None:
    sessions = codex_home / "sessions"
    if not sessions.is_dir():
        return None
    files = list(sessions.glob("**/rollout-*.jsonl"))
    if not files:
        return None
    return max(files, key=lambda path: path.stat().st_mtime)


def read_last_codex_rate_limits(path: Path) -> tuple[dict[str, Any], float | None]:
    last_payload: dict[str, Any] | None = None
    last_as_of: float | None = None
    fallback = path.stat().st_mtime
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict) or obj.get("type") != "event_msg":
                continue
            payload = obj.get("payload")
            if not isinstance(payload, dict):
                continue
            if payload.get("type") != "token_count":
                continue
            rate_limits = payload.get("rate_limits")
            if not isinstance(rate_limits, dict):
                continue
            last_payload = payload
            last_as_of = _line_timestamp(obj, fallback)
    if last_payload is None:
        raise ValueError("no rate limit events")
    return last_payload, last_as_of


def load_codex(paths: LimitsPaths, *, now: datetime | None = None) -> tuple[list[str], dict[str, Any]]:
    now = now or datetime.now().astimezone()
    try:
        rollout = find_newest_rollout_file(paths.codex_home)
        if rollout is None:
            raise ValueError("no session logs")
        payload, as_of_ts = read_last_codex_rate_limits(rollout)
        rate_limits = payload["rate_limits"]
        if not isinstance(rate_limits, dict):
            raise TypeError("invalid rate_limits")
        primary = rate_limits.get("primary") or {}
        secondary = rate_limits.get("secondary") or {}
        plan_type = payload.get("plan_type") or rate_limits.get("plan_type") or "unknown"
        credits = payload.get("credits") or rate_limits.get("credits") or {}

        lines = [f"Codex ({plan_type})"]
        if primary:
            lines.append(
                _format_window_line(
                    "5-hour",
                    float(primary.get("used_percent", 0)),
                    int(primary.get("resets_at", 0)),
                    now=now,
                )
            )
        if secondary:
            lines.append(
                _format_window_line(
                    "weekly",
                    float(secondary.get("used_percent", 0)),
                    int(secondary.get("resets_at", 0)),
                    now=now,
                )
            )
        as_of = datetime.fromtimestamp(as_of_ts or rollout.stat().st_mtime, tz=timezone.utc).astimezone()
        if isinstance(credits, dict) and credits.get("has_credits"):
            balance = credits.get("balance")
            if balance is not None and not credits.get("unlimited"):
                lines.append(f"  credits  {balance}")
        lines.append(f"  as of {format_local_day_time(as_of)} (last Codex call)")

        json_body: dict[str, Any] = {
            "plan_type": plan_type,
            "as_of": int(as_of_ts or rollout.stat().st_mtime),
            "primary": primary,
            "secondary": secondary,
        }
        if isinstance(credits, dict) and credits.get("has_credits"):
            json_body["credits"] = {
                key: credits[key]
                for key in ("has_credits", "unlimited", "balance")
                if key in credits
            }
        return lines, json_body
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        return [f"Codex: unavailable ({reason})"], {"error": reason}


def read_cursor_access_token(state_db: Path) -> str:
    if not state_db.is_file():
        raise OSError("state database missing")
    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".vscdb", delete=False) as tmp:
            tmp_path = tmp.name
        shutil.copy2(state_db, tmp_path)
        conn = sqlite3.connect(tmp_path)
        try:
            row = conn.execute(
                "SELECT value FROM ItemTable WHERE key = ?",
                (CURSOR_TOKEN_KEY,),
            ).fetchone()
        finally:
            conn.close()
    finally:
        if tmp_path:
            Path(tmp_path).unlink(missing_ok=True)
    if not row or row[0] in (None, b"", ""):
        raise ValueError("not signed in")
    value = row[0]
    if isinstance(value, bytes):
        return value.decode("utf-8")
    return str(value)


def fetch_cursor_period_usage(
    token: str,
    *,
    opener: UrlOpener | None = None,
    url: str = CURSOR_USAGE_URL,
    timeout: float = 20.0,
) -> dict[str, Any]:
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(
        url,
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Connect-Protocol-Version": "1",
        },
        method="POST",
    )
    with opener(request, timeout=timeout) as response:
        body = response.read()
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("unexpected response")
    return parsed


def _billing_cycle_end_ms(raw: Any) -> int:
    if isinstance(raw, str):
        return int(raw.strip())
    if isinstance(raw, (int, float)):
        return int(raw)
    raise ValueError("missing billing cycle")


def _days_left(until: datetime, *, now: datetime) -> int:
    seconds = (until - now).total_seconds()
    if seconds <= 0:
        return 0
    return max(0, math.ceil(seconds / 86400))


def _format_on_demand(spend_limit_usage: Any) -> tuple[str, dict[str, Any] | None]:
    if not isinstance(spend_limit_usage, dict):
        return "disabled", None
    limit = spend_limit_usage.get("limit")
    if limit in (None, 0, "", False):
        return "disabled", None
    used = spend_limit_usage.get("percentUsed")
    if used is None:
        used = spend_limit_usage.get("usedPercent")
    pct = int(round(float(used or 0)))
    return f"{pct}% used", {"percent_used": pct, "limit": limit}


def _sanitize_cursor_error(exc: BaseException, token: str | None) -> str:
    reason = str(exc) or exc.__class__.__name__
    if token and token in reason:
        return "request failed"
    if "Bearer" in reason:
        return "request failed"
    return reason


def load_cursor(
    paths: LimitsPaths,
    *,
    now: datetime | None = None,
    opener: UrlOpener | None = None,
) -> tuple[list[str], dict[str, Any]]:
    now = now or datetime.now().astimezone()
    token: str | None = None
    try:
        token = read_cursor_access_token(paths.cursor_state_db)
        payload = fetch_cursor_period_usage(token, opener=opener)
        end_ms = _billing_cycle_end_ms(payload.get("billingCycleEnd"))
        end_dt = datetime.fromtimestamp(end_ms / 1000.0, tz=timezone.utc).astimezone()
        plan_usage = payload.get("planUsage") or {}
        auto_used = int(round(float(plan_usage.get("autoPercentUsed") or 0)))
        api_used = int(round(float(plan_usage.get("apiPercentUsed") or 0)))
        on_demand_text, on_demand_json = _format_on_demand(payload.get("spendLimitUsage"))
        days = _days_left(end_dt, now=now)
        lines = [
            f"Cursor (monthly)  resets {format_local_date(end_dt)} ({days} days left)",
            f"  Cursor models (Composer, Grok)  {auto_used}% used",
            f"  Other models                     {api_used}% used",
            f"  On-demand spending              {on_demand_text}",
        ]
        json_body: dict[str, Any] = {
            "billing_cycle_end_ms": end_ms,
            "days_left": days,
            "auto_percent_used": auto_used,
            "api_percent_used": api_used,
            "on_demand": on_demand_json or {"disabled": True},
        }
        return lines, json_body
    except urllib.error.HTTPError as exc:
        reason = f"HTTP {exc.code}"
        return [f"Cursor: unavailable ({reason})"], {"error": reason}
    except (OSError, ValueError, urllib.error.URLError, json.JSONDecodeError) as exc:
        reason = _sanitize_cursor_error(exc, token)
        return [f"Cursor: unavailable ({reason})"], {"error": reason}
    except Exception as exc:
        reason = _sanitize_cursor_error(exc, token)
        return [f"Cursor: unavailable ({reason})"], {"error": reason}


def load_claude(paths: LimitsPaths, *, now: datetime | None = None) -> tuple[list[str], dict[str, Any]]:
    now = now or datetime.now().astimezone()
    cache_path = paths.claude_usage_cache
    if not cache_path.is_file():
        message = "no data yet (Claude Code saves it on its next reply)"
        return [f"Claude: {message}"], {"error": message}
    try:
        payload = json.loads(cache_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("invalid cache")
        saved_at = int(payload.get("saved_at", 0))
        rate_limits = payload.get("rate_limits") or {}
        lines = ["Claude"]
        windows: dict[str, Any] = {}
        mapping = (("five_hour", "5-hour"), ("seven_day", "weekly"))
        for key, label in mapping:
            window = rate_limits.get(key)
            if not isinstance(window, dict):
                continue
            used = float(window.get("used_percentage", 0))
            resets_at = int(window.get("resets_at", 0))
            lines.append(_format_window_line(label, used, resets_at, now=now))
            windows[key] = {
                "used_percentage": used,
                "resets_at": resets_at,
            }
        as_of = datetime.fromtimestamp(saved_at, tz=timezone.utc).astimezone()
        lines.append(f"  as of {format_local_day_time(as_of)} (last Claude Code reply)")
        return lines, {"saved_at": saved_at, "windows": windows}
    except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
        reason = str(exc) or exc.__class__.__name__
        return [f"Claude: unavailable ({reason})"], {"error": reason}


def render_limits(
    paths: LimitsPaths,
    *,
    as_json: bool = False,
    now: datetime | None = None,
    opener: UrlOpener | None = None,
) -> str:
    now = now or datetime.now().astimezone()
    codex_lines, codex_json = load_codex(paths, now=now)
    cursor_lines, cursor_json = load_cursor(paths, now=now, opener=opener)
    claude_lines, claude_json = load_claude(paths, now=now)
    if as_json:
        return json.dumps(
            {"codex": codex_json, "cursor": cursor_json, "claude": claude_json},
            indent=2,
            sort_keys=True,
        )
    blocks = ["\n".join(codex_lines), "\n".join(cursor_lines), "\n".join(claude_lines)]
    return "\n\n".join(blocks)


def run_limits(
    *,
    as_json: bool = False,
    paths: LimitsPaths | None = None,
    opener: UrlOpener | None = None,
) -> int:
    paths = paths or LimitsPaths.from_env()
    print(render_limits(paths, as_json=as_json, opener=opener))
    return 0
