from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import time
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import HarnessConfig
from .state import StateStore
from .models import TaskStatus

_log = logging.getLogger(__name__)


def _warn_keep_awake_failure() -> None:
    _log.warning("keep-awake helper failed; continuing without sleep prevention")


def start_macos_keep_awake(
    *,
    pid: int | None = None,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> subprocess.Popen[bytes] | None:
    harness_pid = os.getpid() if pid is None else pid
    return popen(
        ["caffeinate", "-i", "-w", str(harness_pid)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def start_linux_keep_awake(
    *,
    which: Callable[[str], str | None] | None = None,
    popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
) -> subprocess.Popen[bytes] | None:
    resolver = shutil.which if which is None else which
    inhibit = resolver("systemd-inhibit")
    if inhibit is None:
        return None
    return popen(
        [inhibit, "--what=idle:sleep", "--why=agent harness run", "sleep", "infinity"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def stop_unix_keep_awake(proc: subprocess.Popen[bytes]) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


@dataclass(slots=True)
class QuotaStatus:
    policy: str
    reset_at: str | None
    source: str
    waiting_tasks: list[str]


class KeepAwake(AbstractContextManager["KeepAwake"]):
    """Best-effort temporary sleep prevention on Windows, macOS, and Linux.

    Contexts are process-local and nest safely. This matters because a full
    `run`/`resume` now owns a keep-awake context while quota waiting may enter a
    second context. Only the outermost active context stops the platform helper
    or releases the Windows execution-state requirement.
    """

    ES_CONTINUOUS = 0x80000000
    ES_SYSTEM_REQUIRED = 0x00000001
    _lock = threading.RLock()
    _depth = 0
    _windows_active = False
    _unix_proc: subprocess.Popen[bytes] | None = None

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self._entered_active = False

    @classmethod
    def _session_active(cls) -> bool:
        if os.name == "nt":
            return cls._windows_active
        return cls._unix_proc is not None

    def __enter__(self) -> "KeepAwake":
        if not self.enabled:
            return self
        with self._lock:
            if self.__class__._depth == 0:
                if os.name == "nt":
                    try:
                        windll = getattr(ctypes, "windll", None)
                        if windll is None:
                            return self
                        result = windll.kernel32.SetThreadExecutionState(
                            self.ES_CONTINUOUS | self.ES_SYSTEM_REQUIRED
                        )
                        self.__class__._windows_active = bool(result)
                    except Exception:
                        self.__class__._windows_active = False
                elif sys.platform == "darwin":
                    try:
                        self.__class__._unix_proc = start_macos_keep_awake()
                    except Exception:
                        self.__class__._unix_proc = None
                        _warn_keep_awake_failure()
                elif sys.platform.startswith("linux"):
                    try:
                        self.__class__._unix_proc = start_linux_keep_awake()
                    except Exception:
                        self.__class__._unix_proc = None
                        _warn_keep_awake_failure()
            if self._session_active():
                self.__class__._depth += 1
                self._entered_active = True
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._entered_active:
            return
        with self._lock:
            self.__class__._depth = max(0, self.__class__._depth - 1)
            if self.__class__._depth == 0:
                if os.name == "nt" and self.__class__._windows_active:
                    try:
                        windll = getattr(ctypes, "windll", None)
                        if windll is not None:
                            windll.kernel32.SetThreadExecutionState(self.ES_CONTINUOUS)
                    except Exception:
                        pass
                    self.__class__._windows_active = False
                elif self.__class__._unix_proc is not None:
                    try:
                        stop_unix_keep_awake(self.__class__._unix_proc)
                    except Exception:
                        _warn_keep_awake_failure()
                    self.__class__._unix_proc = None
        self._entered_active = False


class QuotaManager:
    RESET_META_KEY = "quota.reset_at_override"

    def __init__(self, config: HarnessConfig, state: StateStore):
        self.config = config
        self.state = state

    @staticmethod
    def _parse_iso(value: str) -> datetime:
        text = value.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def configured_reset_at(self) -> tuple[str | None, str]:
        override = self.state.get_meta(self.RESET_META_KEY)
        if isinstance(override, str) and override.strip():
            return override, "runtime override"
        if self.config.quota.reset_at:
            return self.config.quota.reset_at, "harness.yaml"
        return None, "unknown"

    def set_reset_at(self, value: str) -> str:
        parsed = self._parse_iso(value)
        normalized = parsed.isoformat()
        self.state.set_meta(self.RESET_META_KEY, normalized)
        return normalized

    def clear_reset_at(self) -> None:
        self.state.set_meta(self.RESET_META_KEY, None)

    def waiting_tasks(self) -> list[str]:
        return sorted(
            task_id
            for task_id, runtime in self.state.all().items()
            if runtime.status == TaskStatus.WAITING_QUOTA
        )

    def status(self) -> QuotaStatus:
        reset_at, source = self.configured_reset_at()
        return QuotaStatus(
            policy=self.config.quota.policy,
            reset_at=reset_at,
            source=source,
            waiting_tasks=self.waiting_tasks(),
        )

    def next_wait_seconds(self, *, now: datetime | None = None) -> float:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        reset_at, _ = self.configured_reset_at()
        if reset_at:
            target = self._parse_iso(reset_at) + timedelta(minutes=max(0.0, self.config.quota.reset_grace_minutes))
            if target > now:
                return max(1.0, (target - now).total_seconds())
        return max(1.0, self.config.quota.probe_interval_minutes * 60.0)

    def wait_once(self, *, sprint_id: str, sleep_fn=time.sleep) -> float:
        seconds = self.next_wait_seconds()
        self.state.set_meta(f"{sprint_id}.run_status", TaskStatus.WAITING_QUOTA.value)
        with KeepAwake(self.config.quota.keep_awake):
            # Sleep in chunks so Ctrl+C remains responsive on platforms where
            # a very long sleep is awkward to interrupt.
            remaining = seconds
            while remaining > 0:
                chunk = min(remaining, 60.0)
                sleep_fn(chunk)
                remaining -= chunk
        return seconds
