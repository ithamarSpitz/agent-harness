from __future__ import annotations

import os
import shutil
import sys


_SUCCESS_CACHE: dict[str, str] = {}


def _registry_path() -> str:
    """Return the current Windows PATH recorded for the user and machine."""

    import winreg

    locations = (
        (winreg.HKEY_CURRENT_USER, r"Environment"),
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
    )
    values: list[str] = []
    for hive, key_name in locations:
        try:
            with winreg.OpenKey(hive, key_name) as key:
                value, _value_type = winreg.QueryValueEx(key, "Path")
        except OSError:
            continue
        if isinstance(value, str) and value:
            values.append(os.path.expandvars(value))
    return os.pathsep.join(values)


def locate(command: str) -> str | None:
    """Locate a command, including Windows PATH updates made after startup."""

    cached = _SUCCESS_CACHE.get(command)
    if cached is not None:
        return cached

    found = shutil.which(command)
    if found is not None:
        _SUCCESS_CACHE[command] = found
        return found
    if sys.platform != "win32":
        return None

    registry_path = _registry_path()
    if not registry_path:
        return None
    found = shutil.which(command, path=registry_path)
    if found is not None:
        _SUCCESS_CACHE[command] = found
    return found


def clear_locate_cache() -> None:
    """Clear paths cached by successful command lookups."""

    _SUCCESS_CACHE.clear()
