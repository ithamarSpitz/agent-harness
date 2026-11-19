from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone

from harness.process_utils import prepare_external_argv


DEFAULT_COMMAND = ["kiro-cli", "chat", "--no-interactive", "/usage"]

_ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_RESET_DATE = re.compile(r"resets on (\d{4}-\d{2}-\d{2})")
_CREDITS = re.compile(r"Credits \(([\d.]+) of ([\d.]+) covered in plan\)")


@dataclass(frozen=True, slots=True)
class KiroQuota:
    plan: str
    used: float
    limit: float
    used_percent: float
    reset_time: datetime


class KiroUsageError(RuntimeError):
    """Raised when Kiro monthly credit usage cannot be read."""


def parse_kiro_usage(text: str) -> KiroQuota:
    """Parse monthly credit usage from Kiro's ``/usage`` output."""

    clean_text = _ANSI_ESCAPE.sub("", text)

    reset_match = _RESET_DATE.search(clean_text)
    if reset_match is None:
        raise KiroUsageError("missing reset date")
    try:
        reset_time = datetime.strptime(reset_match.group(1), "%Y-%m-%d").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise KiroUsageError("invalid reset date") from exc

    credits_match = _CREDITS.search(clean_text)
    if credits_match is None:
        raise KiroUsageError("missing credits")
    try:
        used = float(credits_match.group(1))
        limit = float(credits_match.group(2))
    except ValueError as exc:
        raise KiroUsageError("invalid credits") from exc
    if limit <= 0:
        raise KiroUsageError("invalid limit")

    plan = ""
    for line in clean_text.splitlines():
        if "Estimated Usage" in line and "|" in line:
            plan = line.rsplit("|", 1)[-1].strip()
    if not plan:
        raise KiroUsageError("missing plan")

    return KiroQuota(
        plan=plan,
        used=used,
        limit=limit,
        used_percent=min(100.0, used / limit * 100),
        reset_time=reset_time,
    )


def _first_stderr_line(stderr: str | bytes | None, fallback: str) -> str:
    if isinstance(stderr, bytes):
        stderr = stderr.decode(errors="replace")
    if stderr:
        lines = stderr.splitlines()
        if lines:
            return lines[0]
    return fallback


def fetch_kiro_usage(
    command: list[str] | None = None,
    timeout: int = 60,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> KiroQuota:
    """Fetch Kiro monthly credit usage using the Kiro CLI."""

    argv = prepare_external_argv(list(DEFAULT_COMMAND if command is None else command))
    try:
        completed = run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            shell=False,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        reason = _first_stderr_line(exc.stderr, "command timed out")
        raise KiroUsageError(reason) from exc
    except OSError as exc:
        reason = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
        raise KiroUsageError(reason) from exc

    if completed.returncode != 0:
        reason = _first_stderr_line(completed.stderr, "command failed")
        raise KiroUsageError(reason)
    return parse_kiro_usage(completed.stdout)
