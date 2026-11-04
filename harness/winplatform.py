"""Keep Python's ``platform`` module from blocking on a stuck Windows WMI service."""

from __future__ import annotations

import platform
import sys


def _wmi_disabled(*_args: object, **_kwargs: object) -> None:
    raise OSError("WMI disabled by harness")


def disable_wmi_queries() -> None:
    """Make ``platform`` use its non-blocking fallbacks instead of WMI (Windows only)."""
    if sys.platform != "win32":
        return
    if not hasattr(platform, "_wmi_query"):
        return
    setattr(platform, "_wmi_query", _wmi_disabled)
