from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from pathlib import Path
from typing import Any

from .logging_utils import utc_now


def count_code_map_calls(commands: list[str], configured_command: str) -> int:
    """Count executed shell commands containing the configured command as a word."""
    command = configured_command.strip()
    if not command:
        return 0
    pattern = re.compile(rf"(?<![\w-]){re.escape(command)}(?![\w-])")
    return sum(1 for line in commands if pattern.search(line))


class UsageRecorder:
    """Append-only local accounting for agent calls.

    Records provider-reported token usage and observed agent activity alongside
    call metadata and outcomes.
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def record(self, event: dict[str, Any]) -> None:
        payload = {"recorded_at": utc_now(), **event}
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, sort_keys=True) + "\n")

    def events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        result: list[dict[str, Any]] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                result.append(json.loads(line))
        return result

    @staticmethod
    def _empty_row() -> dict[str, float | int]:
        return {
            "calls": 0, "seconds": 0.0, "failures": 0, "stalls": 0, "timeouts": 0,
            "quota_pauses": 0, "capacity_pauses": 0, "transient_failures": 0,
            "waiting_network": 0, "waiting_provider_busy": 0,
            "waiting_provider_error": 0, "waiting_timeout": 0,
            "provider_switches": 0, "input_tokens": 0, "output_tokens": 0,
        }

    @classmethod
    def _add_event(cls, row: dict[str, float | int], event: dict[str, Any]) -> None:
        if event.get("event_type") != "provider_switch":
            row["calls"] = int(row["calls"]) + 1
        if event.get("event_type") == "provider_switch":
            row["provider_switches"] = int(row["provider_switches"]) + 1
        row["seconds"] = float(row["seconds"]) + float(event.get("duration_seconds", 0.0))
        if not event.get("ok", False):
            row["failures"] = int(row["failures"]) + 1
        if event.get("stalled", False):
            row["stalls"] = int(row["stalls"]) + 1
        if event.get("timed_out", False):
            row["timeouts"] = int(row["timeouts"]) + 1
        if event.get("quota_exhausted", False):
            row["quota_pauses"] = int(row["quota_pauses"]) + 1
        if event.get("capacity_exhausted", False):
            row["capacity_pauses"] = int(row["capacity_pauses"]) + 1
        if event.get("transient_error", False):
            row["transient_failures"] = int(row["transient_failures"]) + 1
        cause = event.get("waiting_cause")
        cause_key = f"waiting_{cause}"
        if cause in {"network", "provider_busy", "provider_error", "timeout"}:
            row[cause_key] = int(row[cause_key]) + 1
        row["input_tokens"] = int(row["input_tokens"]) + int(event.get("input_tokens") or 0)
        row["output_tokens"] = int(row["output_tokens"]) + int(event.get("output_tokens") or 0)

    def summary(self) -> dict[str, dict[str, float | int]]:
        """Legacy per-model summary retained for tests/scripts predating providers."""
        groups: dict[str, dict[str, float | int]] = defaultdict(self._empty_row)
        for event in self.events():
            key = str(event.get("model") or event.get("model_class") or event.get("event_type") or "default")
            self._add_event(groups[key], event)
        return dict(groups)

    def summary_by_provider(self) -> dict[str, dict[str, float | int]]:
        groups: dict[str, dict[str, float | int]] = defaultdict(self._empty_row)
        for event in self.events():
            provider = str(event.get("provider") or "cursor")
            model = str(event.get("model") or event.get("model_class") or event.get("event_type") or "default")
            self._add_event(groups[f"{provider}:{model}"], event)
        return dict(groups)
