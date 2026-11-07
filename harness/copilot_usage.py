from __future__ import annotations

import json
import math
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


DEFAULT_COMMAND = ["gh", "api", "copilot_internal/user"]


@dataclass(frozen=True, slots=True)
class CopilotQuota:
    plan: str
    entitlement: int
    remaining: int
    percent_remaining: float
    unlimited: bool
    reset_time: datetime


class CopilotUsageError(RuntimeError):
    """Raised when Copilot premium-request quota cannot be read."""


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CopilotUsageError(f"invalid {field}")
    return value


def _percentage(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CopilotUsageError("invalid percent_remaining")
    result = float(value)
    if not math.isfinite(result) or not 0 <= result <= 100:
        raise CopilotUsageError("invalid percent_remaining")
    return result


def _reset_time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise CopilotUsageError("invalid quota_reset_date_utc")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (OverflowError, ValueError) as exc:
        raise CopilotUsageError("invalid quota_reset_date_utc") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise CopilotUsageError("invalid quota_reset_date_utc")
    return parsed.astimezone(timezone.utc)


def parse_copilot_user(text: str) -> CopilotQuota:
    """Parse the premium-interaction quota from ``gh api`` output."""

    try:
        payload = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise CopilotUsageError("invalid JSON") from exc
    if not isinstance(payload, dict):
        raise CopilotUsageError("invalid response shape")

    plan = payload.get("copilot_plan")
    if not isinstance(plan, str) or not plan.strip():
        raise CopilotUsageError("invalid copilot_plan")

    snapshots = payload.get("quota_snapshots")
    if not isinstance(snapshots, dict):
        raise CopilotUsageError("missing quota_snapshots")
    premium = snapshots.get("premium_interactions")
    if not isinstance(premium, dict):
        raise CopilotUsageError("missing premium_interactions")

    unlimited = premium.get("unlimited")
    if not isinstance(unlimited, bool):
        raise CopilotUsageError("invalid unlimited")

    return CopilotQuota(
        plan=plan,
        entitlement=_integer(premium.get("entitlement"), "entitlement"),
        remaining=_integer(premium.get("remaining"), "remaining"),
        percent_remaining=_percentage(premium.get("percent_remaining")),
        unlimited=unlimited,
        reset_time=_reset_time(payload.get("quota_reset_date_utc")),
    )


def _first_stderr_line(stderr: str | bytes | None, fallback: str) -> str:
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    if stderr:
        lines = stderr.splitlines()
        if lines:
            return lines[0]
    return fallback


def fetch_copilot_quota(
    command: list[str] | None = None,
    timeout: int = 30,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> CopilotQuota:
    """Fetch Copilot premium-request quota using the GitHub CLI."""

    argv = list(DEFAULT_COMMAND if command is None else command)
    try:
        completed = run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        reason = _first_stderr_line(exc.stderr, "command timed out")
        raise CopilotUsageError(reason) from exc
    except OSError as exc:
        reason = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        raise CopilotUsageError(reason) from exc

    if completed.returncode != 0:
        reason = _first_stderr_line(completed.stderr, "command failed")
        raise CopilotUsageError(reason)
    return parse_copilot_user(completed.stdout)
