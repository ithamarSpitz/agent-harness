from __future__ import annotations

import ctypes
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable

_KIB_TO_GB = 1.0 / (1024.0 * 1024.0)
_BYTES_TO_GB = 1.0 / (1024.0**3)

_MEMINFO_AVAILABLE = re.compile(r"^MemAvailable:\s+(\d+)\s+kB\s*$", re.MULTILINE)
_VM_STAT_PAGE_SIZE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_PAGES = re.compile(
    r"^Pages (free|inactive|speculative):\s+(\d+)\.?\s*$",
    re.MULTILINE,
)
_VM_STAT_REQUIRED = frozenset({"free", "inactive", "speculative"})


def linux_free_memory_gb(*, meminfo: str | None = None) -> float | None:
    text = meminfo
    if text is None:
        try:
            text = Path("/proc/meminfo").read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    match = _MEMINFO_AVAILABLE.search(text)
    if not match:
        return None
    try:
        kib = int(match.group(1))
    except ValueError:
        return None
    if kib < 0:
        return None
    return kib * _KIB_TO_GB


def darwin_free_memory_gb(*, vm_stat: str | None = None) -> float | None:
    text = vm_stat
    if text is None:
        try:
            completed = subprocess.run(
                ["vm_stat"],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            return None
        if completed.returncode != 0:
            return None
        text = completed.stdout
    page_size_match = _VM_STAT_PAGE_SIZE.search(text)
    if not page_size_match:
        return None
    try:
        page_size = int(page_size_match.group(1))
    except ValueError:
        return None
    if page_size <= 0:
        return None
    counts: dict[str, int] = {}
    for name, count_text in _VM_STAT_PAGES.findall(text):
        try:
            counts[name] = int(count_text)
        except ValueError:
            return None
    if not _VM_STAT_REQUIRED.issubset(counts):
        return None
    pages = sum(counts[name] for name in _VM_STAT_REQUIRED)
    if pages < 0:
        return None
    return pages * page_size * _BYTES_TO_GB


class _MemoryStatusEx(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


def _default_windows_avail_phys_bytes() -> int | None:
    try:
        stat = _MemoryStatusEx()
        stat.dwLength = ctypes.sizeof(_MemoryStatusEx)
        kernel32 = getattr(getattr(ctypes, "windll", None), "kernel32", None)
        if kernel32 is None:
            return None
        ok = kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
        if not ok:
            return None
        return int(stat.ullAvailPhys)
    except Exception:
        return None


def windows_free_memory_gb(
    *,
    avail_phys_bytes: int | None = None,
    query_avail_phys: Callable[[], int | None] | None = None,
) -> float | None:
    if avail_phys_bytes is not None:
        if avail_phys_bytes < 0:
            return None
        return avail_phys_bytes * _BYTES_TO_GB
    query = query_avail_phys or _default_windows_avail_phys_bytes
    value = query()
    if value is None or value < 0:
        return None
    return value * _BYTES_TO_GB


def free_memory_gb() -> float | None:
    try:
        if sys.platform == "win32":
            return windows_free_memory_gb()
        if sys.platform == "darwin":
            return darwin_free_memory_gb()
        if sys.platform.startswith("linux"):
            return linux_free_memory_gb()
        return None
    except Exception:
        return None
