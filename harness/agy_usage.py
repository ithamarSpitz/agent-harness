from __future__ import annotations

import json
import math
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True, slots=True)
class AgyBucket:
    pool_id: str
    group_name: str
    window: str
    remaining_fraction: float
    reset_time: datetime


class AgyUsageError(RuntimeError):
    """Raised when the Antigravity usage command cannot be completed."""


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _remaining_fraction(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 1:
        return None
    return result


def _utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _parse_bucket(raw: Any, group_name: str) -> AgyBucket | None:
    if not isinstance(raw, dict):
        return None
    pool_id = _nonempty_string(raw.get("id"))
    window = _nonempty_string(raw.get("window"))
    remaining = _remaining_fraction(raw.get("remaining_fraction"))
    reset_time = _utc_datetime(raw.get("reset_time"))
    if pool_id is None or window is None or remaining is None or reset_time is None:
        return None
    return AgyBucket(pool_id, group_name, window, remaining, reset_time)


def parse_agy_usage(text: str) -> list[AgyBucket]:
    """Parse valid quota buckets from ``agy -p /usage`` JSON output."""

    try:
        payload = json.loads(text)
        groups = payload["command"]["data"]["groups"]
        if not isinstance(groups, list):
            return []
        result: list[AgyBucket] = []
        for group in groups:
            if not isinstance(group, dict):
                continue
            group_name = _nonempty_string(group.get("name"))
            buckets = group.get("buckets")
            if group_name is None or not isinstance(buckets, list):
                continue
            for raw_bucket in buckets:
                bucket = _parse_bucket(raw_bucket, group_name)
                if bucket is not None:
                    result.append(bucket)
        return result
    except Exception:
        # Provider output is external data. A malformed response means there are
        # no trustworthy buckets, not that callers should fail.
        return []


def fetch_agy_usage(
    command: str = "agy",
    timeout: float = 30,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[AgyBucket]:
    """Fetch Antigravity quota in an isolated directory with stdin closed."""

    with tempfile.TemporaryDirectory(
        prefix="harness-agy-usage-", ignore_cleanup_errors=True
    ) as cwd:
        try:
            completed = run(
                [command, "-p", "/usage", "--output-format", "json"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=cwd,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AgyUsageError(
                f"agy usage command timed out after {timeout:g} seconds"
            ) from exc
        except OSError as exc:
            reason = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
            raise AgyUsageError(f"agy usage command failed to start: {reason}") from exc

    if completed.returncode != 0:
        raise AgyUsageError(f"agy usage command exited with status {completed.returncode}")
    return parse_agy_usage(completed.stdout)
