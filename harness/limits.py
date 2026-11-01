from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .process_utils import prepare_external_argv, resolve_codex_argv, stop_process_tree

CURSOR_USAGE_URL = (
    "https://api2.cursor.sh/aiserver.v1.DashboardService/GetCurrentPeriodUsage"
)
CURSOR_TOKEN_KEY = "cursorAuth/accessToken"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
CLAUDE_OAUTH_BETA = "oauth-2025-04-20"


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


class _ClaudeNoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


def _open_claude_usage(request: urllib.request.Request, *, timeout: float) -> Any:
    opener = urllib.request.build_opener(_ClaudeNoRedirectHandler())
    return opener.open(request, timeout=timeout)


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


def format_short_month_day(when: datetime) -> str:
    local = when.astimezone()
    return local.strftime("%b %d")


def _format_duration_until(*, seconds: int) -> str:
    if seconds >= 86400:
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        return f"in {days}d {hours}h"
    hours = seconds // 3600
    minutes = (seconds % 3600) // 60
    return f"in {hours}h {minutes}m"


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
    remaining = int((reset_dt - now).total_seconds())
    pct = int(round(used_percent))
    return f"  {label:<8} {pct}% used, resets {reset_label} ({_format_duration_until(seconds=remaining)})"


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


class _StdoutLineQueue:
    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._lines: list[str] = []
        self._lock = threading.Lock()
        self._proc = proc
        thread = threading.Thread(target=self._reader, daemon=True)
        thread.start()

    def _reader(self) -> None:
        assert self._proc.stdout is not None
        for raw in self._proc.stdout:
            with self._lock:
                self._lines.append(raw)

    def wait_for_result(self, request_id: int, *, deadline: float) -> dict[str, Any]:
        index = 0
        while time.monotonic() < deadline:
            with self._lock:
                pending = self._lines[index:]
                index = len(self._lines)
            for line in pending:
                text = line.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict) or obj.get("id") != request_id:
                    continue
                if "error" in obj:
                    raise ValueError(str(obj["error"]))
                result = obj.get("result")
                if isinstance(result, dict):
                    return result
                raise ValueError("invalid response")
            if self._proc.poll() is not None:
                with self._lock:
                    drained = index >= len(self._lines)
                if drained:
                    raise ValueError("codex app-server exited")
            time.sleep(0.05)
        raise TimeoutError("codex app-server timed out")


def _send_json_rpc(proc: subprocess.Popen[str], message: dict[str, Any]) -> None:
    if proc.stdin is None:
        raise OSError("codex app-server stdin unavailable")
    proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
    proc.stdin.flush()


def fetch_codex_live_rate_limits(
    *,
    codex_command: str = "codex",
    timeout: float = 20.0,
    harness_version: str | None = None,
) -> dict[str, Any]:
    """Query Codex account rate limits through ``codex app-server`` JSON-RPC."""
    argv = prepare_external_argv(resolve_codex_argv(codex_command, "app-server"))
    version = harness_version if harness_version is not None else __version__
    creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=(os.name != "nt"),
        creationflags=creationflags,
    )
    deadline = time.monotonic() + timeout
    try:
        stdout = _StdoutLineQueue(proc)
        _send_json_rpc(
            proc,
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {
                        "name": "harness-limits",
                        "title": "harness limits",
                        "version": version,
                    }
                },
            },
        )
        stdout.wait_for_result(1, deadline=deadline)
        _send_json_rpc(proc, {"method": "initialized"})
        _send_json_rpc(proc, {"id": 2, "method": "account/rateLimits/read"})
        return stdout.wait_for_result(2, deadline=deadline)
    finally:
        stop_process_tree(proc)


def _live_window_dict(window: dict[str, Any] | None) -> dict[str, Any]:
    if not isinstance(window, dict):
        return {}
    used = window.get("usedPercent", window.get("used_percent", 0))
    resets = window.get("resetsAt", window.get("resets_at", 0))
    return {
        "used_percent": float(used if used is not None else 0),
        "resets_at": int(resets if resets is not None else 0),
        "window_duration_mins": window.get("windowDurationMins", window.get("window_minutes")),
    }


def _format_reset_credits_line(reset_payload: dict[str, Any]) -> str | None:
    available = reset_payload.get("availableCount", 0)
    if not isinstance(available, int):
        try:
            available = int(available)
        except (TypeError, ValueError):
            return None
    if available <= 0:
        return None
    credits = reset_payload.get("credits")
    if not isinstance(credits, list):
        return None
    parts: list[str] = []
    for entry in credits:
        if not isinstance(entry, dict):
            continue
        if entry.get("status") != "available":
            continue
        title = entry.get("title")
        expires_at = entry.get("expiresAt")
        if not title or expires_at is None:
            continue
        expires_ts = _parse_timestamp(expires_at)
        if expires_ts is None:
            continue
        expires_label = format_short_month_day(
            datetime.fromtimestamp(expires_ts, tz=timezone.utc)
        )
        parts.append(f"{title}: expires {expires_label}")
    if not parts:
        return None
    joined = "; ".join(parts)
    return f"  reset credits: {available} available ({joined})"


def _codex_limit_reached(*, live: dict[str, Any], rate_limits: dict[str, Any]) -> bool:
    if live.get("ordinaryUsageAllowed") is False:
        return True
    reached = rate_limits.get("rateLimitReachedType")
    return bool(reached)


def _build_codex_from_live(live: dict[str, Any], *, now: datetime) -> tuple[list[str], dict[str, Any]]:
    rate_limits = live.get("rateLimits") or {}
    if not isinstance(rate_limits, dict):
        raise TypeError("invalid rateLimits")
    primary = _live_window_dict(rate_limits.get("primary"))
    secondary = _live_window_dict(rate_limits.get("secondary"))
    plan_type = rate_limits.get("planType") or "unknown"
    credits = rate_limits.get("credits") or {}
    limit_reached = _codex_limit_reached(live=live, rate_limits=rate_limits)

    header = f"Codex ({plan_type})"
    if limit_reached:
        header = f"{header}  LIMIT REACHED"
    lines = [header]
    if primary:
        lines.append(
            _format_window_line(
                "5-hour",
                primary["used_percent"],
                int(primary["resets_at"]),
                now=now,
            )
        )
    if secondary:
        lines.append(
            _format_window_line(
                "weekly",
                secondary["used_percent"],
                int(secondary["resets_at"]),
                now=now,
            )
        )
    if isinstance(credits, dict) and credits.get("hasCredits"):
        balance = credits.get("balance")
        if balance is not None and not credits.get("unlimited"):
            lines.append(f"  credits  {balance}")
    reset_payload = live.get("rateLimitResetCredits")
    if isinstance(reset_payload, dict):
        reset_line = _format_reset_credits_line(reset_payload)
        if reset_line:
            lines.append(reset_line)

    reset_credits_json: list[dict[str, Any]] = []
    if isinstance(reset_payload, dict):
        raw_credits = reset_payload.get("credits")
        if isinstance(raw_credits, list):
            for entry in raw_credits:
                if not isinstance(entry, dict) or entry.get("status") != "available":
                    continue
                title = entry.get("title")
                expires_at = entry.get("expiresAt")
                if not title or expires_at is None:
                    continue
                expires_ts = _parse_timestamp(expires_at)
                if expires_ts is None:
                    continue
                reset_credits_json.append({"title": str(title), "expires_at": int(expires_ts)})

    json_body: dict[str, Any] = {
        "source": "live",
        "plan_type": plan_type,
        "limit_reached": limit_reached,
        "primary": primary,
        "secondary": secondary,
        "reset_credits": reset_credits_json,
    }
    if isinstance(credits, dict) and credits.get("hasCredits"):
        json_body["credits"] = {
            "has_credits": credits.get("hasCredits"),
            "unlimited": credits.get("unlimited"),
            "balance": credits.get("balance"),
        }
    return lines, json_body


def _build_codex_from_session_log(
    paths: LimitsPaths,
    *,
    now: datetime,
    stale_note: bool,
) -> tuple[list[str], dict[str, Any]]:
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
    if stale_note:
        lines.append(f"  as of {format_local_day_time(as_of)} (from last Codex call, may be stale)")
    else:
        lines.append(f"  as of {format_local_day_time(as_of)} (last Codex call)")

    json_body: dict[str, Any] = {
        "source": "session-log",
        "plan_type": plan_type,
        "limit_reached": False,
        "as_of": int(as_of_ts or rollout.stat().st_mtime),
        "primary": primary,
        "secondary": secondary,
        "reset_credits": [],
    }
    if isinstance(credits, dict) and credits.get("has_credits"):
        json_body["credits"] = {
            key: credits[key]
            for key in ("has_credits", "unlimited", "balance")
            if key in credits
        }
    return lines, json_body


def load_codex(
    paths: LimitsPaths,
    *,
    now: datetime | None = None,
    codex_command: str = "codex",
    codex_live_timeout: float = 20.0,
) -> tuple[list[str], dict[str, Any]]:
    now = now or datetime.now().astimezone()
    try:
        live = fetch_codex_live_rate_limits(
            codex_command=codex_command,
            timeout=codex_live_timeout,
        )
        return _build_codex_from_live(live, now=now)
    except Exception:
        pass
    try:
        return _build_codex_from_session_log(paths, now=now, stale_note=True)
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


def _claude_credentials_path(paths: LimitsPaths) -> Path:
    claude_home = paths.claude_home or paths.claude_usage_cache.parent
    return claude_home / ".credentials.json"


def read_claude_credentials(
    paths: LimitsPaths,
    *,
    now: datetime,
) -> tuple[str, str]:
    payload = json.loads(_claude_credentials_path(paths).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("invalid credentials")
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        raise ValueError("invalid credentials")
    token = oauth.get("accessToken")
    if not isinstance(token, str) or not token:
        raise ValueError("invalid credentials")
    expires_at = _parse_timestamp(oauth.get("expiresAt"))
    if expires_at is None or expires_at <= now.timestamp():
        raise ValueError("expired credentials")
    subscription_type = oauth.get("subscriptionType")
    if not isinstance(subscription_type, str) or not subscription_type:
        subscription_type = "unknown"
    return token, subscription_type


def fetch_claude_live_usage(
    token: str,
    *,
    opener: UrlOpener | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    opener = opener or _open_claude_usage
    request = urllib.request.Request(
        CLAUDE_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": CLAUDE_OAUTH_BETA,
        },
        method="GET",
    )
    try:
        with opener(request, timeout=timeout) as response:
            body = response.read()
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("unexpected response")
        return payload
    except Exception:
        # The opener may include request headers in its exception. Keep the
        # bearer token out of every raised message as well as rendered output.
        raise OSError("Claude usage request failed") from None


def _claude_live_window(window: Any) -> dict[str, Any]:
    if not isinstance(window, dict):
        raise ValueError("invalid window")
    utilization = float(window["utilization"])
    resets_at = _parse_timestamp(window["resets_at"])
    if resets_at is None:
        raise ValueError("invalid reset time")
    return {"used_percentage": utilization, "resets_at": int(resets_at)}


def _build_claude_from_live(
    payload: dict[str, Any],
    *,
    subscription_type: str,
    now: datetime,
) -> tuple[list[str], dict[str, Any]]:
    lines = [f"Claude ({subscription_type})"]
    windows: dict[str, Any] = {}
    mapping = (
        ("five_hour", "5-hour"),
        ("seven_day", "weekly"),
        ("seven_day_opus", "weekly Opus"),
        ("seven_day_sonnet", "weekly Sonnet"),
    )
    for key, label in mapping:
        raw_window = payload.get(key)
        if raw_window is None:
            continue
        window = _claude_live_window(raw_window)
        lines.append(
            _format_window_line(
                label,
                window["used_percentage"],
                window["resets_at"],
                now=now,
            )
        )
        windows[key] = window
    extra_usage = payload.get("extra_usage")
    extra_usage_enabled = isinstance(extra_usage, dict) and extra_usage.get("is_enabled") is True
    if extra_usage_enabled:
        lines.append("  Extra usage: enabled")
    return lines, {
        "source": "live",
        "subscription_type": subscription_type,
        "windows": windows,
        "extra_usage_enabled": extra_usage_enabled,
    }


def _load_claude_status_line(
    paths: LimitsPaths,
    *,
    now: datetime,
) -> tuple[list[str], dict[str, Any]]:
    cache_path = paths.claude_usage_cache
    if not cache_path.is_file():
        message = "no data yet (Claude Code saves it on its next reply)"
        return [f"Claude: {message}"], {"source": "status-line", "error": message}
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
        lines.append(
            f"  as of {format_local_day_time(as_of)} "
            "(from last Claude Code reply, may be stale)"
        )
        return lines, {"source": "status-line", "saved_at": saved_at, "windows": windows}
    except (OSError, ValueError, json.JSONDecodeError, TypeError) as exc:
        reason = str(exc) or exc.__class__.__name__
        return [f"Claude: unavailable ({reason})"], {
            "source": "status-line",
            "error": reason,
        }


def load_claude(
    paths: LimitsPaths,
    *,
    now: datetime | None = None,
    opener: UrlOpener | None = None,
) -> tuple[list[str], dict[str, Any]]:
    now = now or datetime.now().astimezone()
    try:
        token, subscription_type = read_claude_credentials(paths, now=now)
        payload = fetch_claude_live_usage(token, opener=opener)
        return _build_claude_from_live(
            payload,
            subscription_type=subscription_type,
            now=now,
        )
    except Exception:
        return _load_claude_status_line(paths, now=now)


def render_limits(
    paths: LimitsPaths,
    *,
    as_json: bool = False,
    now: datetime | None = None,
    opener: UrlOpener | None = None,
    codex_command: str = "codex",
    codex_live_timeout: float = 20.0,
) -> str:
    now = now or datetime.now().astimezone()
    codex_lines, codex_json = load_codex(
        paths,
        now=now,
        codex_command=codex_command,
        codex_live_timeout=codex_live_timeout,
    )
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
    codex_command: str = "codex",
    codex_live_timeout: float = 20.0,
) -> int:
    paths = paths or LimitsPaths.from_env()
    print(
        render_limits(
            paths,
            as_json=as_json,
            opener=opener,
            codex_command=codex_command,
            codex_live_timeout=codex_live_timeout,
        )
    )
    return 0
