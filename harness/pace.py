from __future__ import annotations

import json
import logging
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable, Mapping

from .home import harness_home
from .routing import _canonical_window

LOGGER = logging.getLogger(__name__)

HISTORY_NAME = "headroom-history.jsonl"
HISTORY_SECONDS = 24 * 3600
REWRITE_INTERVAL_SECONDS = 3600
MIN_PAIR_SECONDS = 600

DEFAULT_PACE: dict[str, float] = {"5-hour": 30.0, "week": 3.0, "month": 1.0}
FALLBACK_PACE = 3.0


def _window_key(name: str) -> str:
    return _canonical_window(name).lower()


def _marker(target: Path) -> Path:
    return target.with_name(target.name + ".rewrite")


def _last_rewrite(target: Path) -> float | None:
    try:
        return float(_marker(target).read_text(encoding="utf-8").strip())
    except (OSError, ValueError, UnicodeError):
        return None


def history_path() -> Path:
    return harness_home() / HISTORY_NAME


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _decode(line: str) -> dict[str, Any] | None:
    """Parse one history line; any line not matching the schema is skipped (None)."""

    try:
        raw = json.loads(line)
    except ValueError:
        return None
    if not isinstance(raw, dict):
        return None
    time = _number(raw.get("time"))
    used = _number(raw.get("used_percent"))
    resets = _number(raw.get("resets_at"))
    provider = raw.get("provider")
    window = raw.get("window")
    if (
        time is None
        or used is None
        or resets is None
        or resets != int(resets)
        or not isinstance(provider, str)
        or not provider
        or not isinstance(window, str)
        or not window
    ):
        return None
    return {
        "time": time,
        "provider": provider,
        "window": window,
        "used_percent": used,
        "resets_at": int(resets),
    }


def _read(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return []
    samples = (_decode(line) for line in text.splitlines() if line.strip())
    return [s for s in samples if s is not None]


def record_samples(
    entries: Iterable[Any],
    *,
    now: float,
    path: Path | None = None,
) -> None:
    """Append one line per known window; prune old lines at most hourly. Never raises."""

    try:
        target = (path or history_path()).expanduser()
        rows = [
            {
                "time": now,
                "provider": entry.provider,
                "window": entry.window,
                "used_percent": entry.used_percent,
                "resets_at": entry.resets_at,
            }
            for entry in entries
            if entry.known and entry.used_percent is not None and entry.resets_at is not None
        ]
        if not rows:
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        last = _last_rewrite(target)
        if last is None or not 0 <= now - last < REWRITE_INTERVAL_SECONDS:
            kept = [s for s in _read(target) if now - s["time"] <= HISTORY_SECONDS]
            kept.extend(rows)
            fd, temp_name = tempfile.mkstemp(prefix="headroom-history-", suffix=".tmp", dir=target.parent)
            temp_path = Path(temp_name)
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    for row in kept:
                        handle.write(json.dumps(row, sort_keys=True) + "\n")
                os.replace(temp_path, target)
            finally:
                temp_path.unlink(missing_ok=True)
            _marker(target).write_text(repr(now), encoding="utf-8")
            return
        with target.open("a", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    except Exception as exc:  # history must never break usage reads
        LOGGER.warning("could not write headroom history: %s", exc)


def full_pace(
    provider: str,
    window: str,
    *,
    now: float,
    history_path: Path | None = None,
    defaults: Mapping[str, float] | None = None,
) -> float:
    """Highest recent rate (percent per hour) of a usage window, or a default."""

    canonical = _window_key(window)
    path = history_path if history_path is not None else globals()["history_path"]()
    samples = sorted(
        (
            s
            for s in _read(Path(path))
            if s["provider"] == provider
            and _window_key(s["window"]) == canonical
            and 0 <= now - s["time"] <= HISTORY_SECONDS
        ),
        key=lambda s: s["time"],
    )
    best = 0.0
    for first, second in zip(samples, samples[1:]):
        seconds = second["time"] - first["time"]
        if seconds < MIN_PAIR_SECONDS or first["resets_at"] != second["resets_at"]:
            continue
        gained = second["used_percent"] - first["used_percent"]
        if gained <= 0:
            continue
        best = max(best, gained / (seconds / 3600))
    if best > 0:
        return best
    table = dict(DEFAULT_PACE)
    for name, value in (defaults or {}).items():
        table[_window_key(name)] = float(value)
    return table.get(canonical, FALLBACK_PACE)


def waste(left_percent: float, hours_to_reset: float, pace: float) -> float:
    return max(0.0, left_percent - hours_to_reset * pace)
