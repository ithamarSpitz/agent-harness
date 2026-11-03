from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from .logging_utils import utc_now

_LOCK = threading.RLock()
_TRIM_TRIGGER = 1000
_TRIM_KEEP = 500


def events_path(root: Path) -> Path:
    return Path(root) / ".harness" / "events.jsonl"


def append_event(root: Path, sprint_id: str, text: str) -> None:
    """Append one board event line; trim the file when it grows past 1000 lines."""
    path = events_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"time": utc_now(), "sprint": sprint_id, "text": text}
    line = json.dumps(payload, sort_keys=True) + "\n"
    with _LOCK:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line)
        raw = path.read_text(encoding="utf-8")
        lines = [entry for entry in raw.splitlines() if entry.strip()]
        if len(lines) > _TRIM_TRIGGER:
            kept = lines[-_TRIM_KEEP:]
            path.write_text("\n".join(kept) + "\n", encoding="utf-8")


def read_events(root: Path, sprint_id: str, limit: int) -> list[dict[str, Any]]:
    """Return events for ``sprint_id``, newest first, capped at ``limit``."""
    path = events_path(root)
    if not path.exists() or limit <= 0:
        return []
    matched: list[dict[str, Any]] = []
    with _LOCK:
        raw = path.read_text(encoding="utf-8")
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(item, dict):
            continue
        if item.get("sprint") != sprint_id:
            continue
        matched.append(item)
    matched.reverse()
    return matched[:limit]
