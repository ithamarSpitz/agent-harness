from __future__ import annotations

import json
import os
import subprocess
import threading
from collections import deque
from dataclasses import dataclass
from typing import Callable

_BYTES_PER_GB = 1024**3

_WIN32_PROCESS_JSON_CMD = (
    "Get-CimInstance Win32_Process | "
    "Select ProcessId,ParentProcessId,WorkingSetSize | ConvertTo-Json"
)


@dataclass(frozen=True, slots=True)
class ProcInfo:
    pid: int
    ppid: int
    working_set_bytes: int


def parse_windows_process_json(text: str) -> list[ProcInfo]:
    stripped = text.strip()
    if not stripped:
        return []
    payload = json.loads(stripped)
    if isinstance(payload, dict):
        rows = [payload]
    elif isinstance(payload, list):
        rows = payload
    else:
        return []
    out: list[ProcInfo] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pid = row.get("ProcessId")
        ppid = row.get("ParentProcessId")
        working_set = row.get("WorkingSetSize")
        if pid is None or ppid is None or working_set is None:
            continue
        out.append(
            ProcInfo(
                pid=int(pid),
                ppid=int(ppid),
                working_set_bytes=int(working_set),
            )
        )
    return out


def parse_posix_ps_output(text: str) -> list[ProcInfo]:
    out: list[ProcInfo] = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
            rss_kib = int(parts[2])
        except ValueError:
            continue
        out.append(
            ProcInfo(
                pid=pid,
                ppid=ppid,
                working_set_bytes=rss_kib * 1024,
            )
        )
    return out


def _run_windows_process_list() -> list[ProcInfo]:
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", _WIN32_PROCESS_JSON_CMD],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.strip() or "powershell failed")
    return parse_windows_process_json(completed.stdout)


def _run_posix_process_list() -> list[ProcInfo]:
    completed = subprocess.run(
        ["ps", "-A", "-o", "pid=,ppid=,rss="],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise OSError(completed.stderr.strip() or "ps failed")
    return parse_posix_ps_output(completed.stdout)


def list_processes() -> list[ProcInfo]:
    if os.name == "nt":
        return _run_windows_process_list()
    return _run_posix_process_list()


def tree_bytes(root_pid: int, processes: list[ProcInfo]) -> int:
    by_pid: dict[int, ProcInfo] = {}
    for proc in processes:
        by_pid[proc.pid] = proc
    children: dict[int, list[int]] = {}
    for proc in processes:
        children.setdefault(proc.ppid, []).append(proc.pid)

    total = 0
    visited: set[int] = set()
    queue: deque[int] = deque([root_pid])
    while queue:
        pid = queue.popleft()
        if pid in visited:
            continue
        visited.add(pid)
        info = by_pid.get(pid)
        if info is not None:
            total += info.working_set_bytes
        for child_pid in children.get(pid, ()):
            if child_pid not in visited:
                queue.append(child_pid)
    return total


class PeakSampler:
    def __init__(
        self,
        root_pid: int,
        interval_seconds: float,
        *,
        lister: Callable[[], list[ProcInfo]] = list_processes,
    ) -> None:
        self._root_pid = root_pid
        self._interval_seconds = interval_seconds
        self._lister = lister
        self._stop_event = threading.Event()
        self._peak_bytes = 0
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._sample_loop, daemon=True)
        self._thread.start()

    def stop(self) -> float:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._interval_seconds)
        with self._lock:
            peak_bytes = self._peak_bytes
        return peak_bytes / _BYTES_PER_GB

    def _sample_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                processes = self._lister()
                sample = tree_bytes(self._root_pid, processes)
            except Exception:
                sample = None
            if sample is not None:
                with self._lock:
                    if sample > self._peak_bytes:
                        self._peak_bytes = sample
            if self._stop_event.wait(self._interval_seconds):
                break
