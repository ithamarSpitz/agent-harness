from __future__ import annotations

import ctypes
import logging
import os
import shutil
import subprocess
import sys
import time
import threading
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .config import HarnessConfig
from .limits import LimitsPaths, load_codex, load_cursor
from .state import StateStore
from .models import TaskStatus

_log = logging.getLogger(__name__)

# Floor and ceiling for waits derived from live provider reset times.
_MIN_WAIT_SECONDS = 60.0
_MAX_LIMITS_WAIT_SECONDS = 24.0 * 3600.0
_LIMITS_GRACE_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class QuotaResetInfo:
    """A provider that is currently out of quota, with its next reset instant."""

    provider: str
    reset_at: datetime


LimitsSource = Callable[[datetime], Sequence[QuotaResetInfo]]


def _format_wait_duration(seconds: float) -> str:
    total = max(0, int(seconds))
    if total >= 86400:
        days = total // 86400
        hours = (total % 86400) // 3600
        return f"in {days}d {hours}h"
    hours = total // 3600
    minutes = (total % 3600) // 60
    return f"in {hours}h {minutes}m"


def format_quota_wait_message(info: QuotaResetInfo, *, now: datetime) -> str:
    """Human wait line, e.g. ``Waiting for codex quota until 17:14 (in 3h 21m)``."""
    local_reset = info.reset_at.astimezone()
    until = local_reset.strftime("%H:%M")
    remaining = (info.reset_at - now).total_seconds()
    return (
        f"Waiting for {info.provider} quota until {until} "
        f"({_format_wait_duration(remaining)})"
    )


def _window_used_percent(window: object) -> float | None:
    if not isinstance(window, dict) or not window:
        return None
    raw = window.get("used_percent", window.get("usedPercent"))
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _window_resets_at(window: object) -> datetime | None:
    if not isinstance(window, dict) or not window:
        return None
    raw = window.get("resets_at", window.get("resetsAt"))
    if raw is None:
        return None
    try:
        ts = float(raw)
    except (TypeError, ValueError):
        return None
    if ts <= 0:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def default_limits_source(now: datetime) -> list[QuotaResetInfo]:
    """Read Codex/Cursor limits and return reset times for exhausted providers.

    Codex: each rate-limit window at 100 % contributes its ``resets_at``.
    Cursor: when auto or API pool is used up, contribute billing cycle end.
    Failures (missing auth, network, parse errors) yield an empty contribution
    so the caller falls back to the configured probe interval.
    """
    paths = LimitsPaths.from_env()
    found: list[QuotaResetInfo] = []
    try:
        _, codex = load_codex(paths, now=now.astimezone())
    except Exception:
        codex = {"error": "unavailable"}
    if isinstance(codex, dict) and "error" not in codex:
        for key in ("primary", "secondary"):
            window = codex.get(key)
            used = _window_used_percent(window)
            reset_at = _window_resets_at(window)
            if used is not None and used >= 100.0 and reset_at is not None:
                found.append(QuotaResetInfo(provider="codex", reset_at=reset_at))
    try:
        _, cursor = load_cursor(paths, now=now.astimezone())
    except Exception:
        cursor = {"error": "unavailable"}
    if isinstance(cursor, dict) and "error" not in cursor:
        try:
            auto_used = float(cursor.get("auto_percent_used") or 0)
            api_used = float(cursor.get("api_percent_used") or 0)
        except (TypeError, ValueError):
            auto_used = 0.0
            api_used = 0.0
        if auto_used >= 100.0 or api_used >= 100.0:
            end_ms = cursor.get("billing_cycle_end_ms")
            try:
                end_ts = float(end_ms) / 1000.0 if end_ms is not None else 0.0
            except (TypeError, ValueError):
                end_ts = 0.0
            if end_ts > 0:
                found.append(
                    QuotaResetInfo(
                        provider="cursor",
                        reset_at=datetime.fromtimestamp(end_ts, tz=timezone.utc),
                    )
                )
    return found


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

    def __init__(
        self,
        config: HarnessConfig,
        state: StateStore,
        *,
        limits_source: LimitsSource | None = None,
    ):
        self.config = config
        self.state = state
        self.limits_source: LimitsSource = (
            default_limits_source if limits_source is None else limits_source
        )
        self.last_wait_message: str | None = None

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

    def _probe_interval_seconds(self) -> float:
        return max(_MIN_WAIT_SECONDS, self.config.quota.probe_interval_minutes * 60.0)

    def _limits_resets(self, now: datetime) -> Sequence[QuotaResetInfo]:
        try:
            return self.limits_source(now)
        except Exception:
            return []

    def next_wait_seconds(self, *, now: datetime | None = None) -> float:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        self.last_wait_message = None

        # Manual yaml/runtime override still wins when set.
        reset_at, _ = self.configured_reset_at()
        if reset_at:
            target = self._parse_iso(reset_at) + timedelta(
                minutes=max(0.0, self.config.quota.reset_grace_minutes)
            )
            if target > now:
                return max(_MIN_WAIT_SECONDS, (target - now).total_seconds())

        future = [
            info
            for info in self._limits_resets(now)
            if info.reset_at.astimezone(timezone.utc) > now
        ]
        if future:
            earliest = min(future, key=lambda info: info.reset_at.astimezone(timezone.utc))
            reset_utc = earliest.reset_at.astimezone(timezone.utc)
            raw = (reset_utc - now).total_seconds() + _LIMITS_GRACE_SECONDS
            seconds = min(_MAX_LIMITS_WAIT_SECONDS, max(_MIN_WAIT_SECONDS, raw))
            self.last_wait_message = format_quota_wait_message(earliest, now=now)
            return seconds

        return self._probe_interval_seconds()

    def wait_once(self, *, sprint_id: str, sleep_fn=time.sleep) -> float:
        seconds = self.next_wait_seconds()
        if self.last_wait_message:
            print(self.last_wait_message, flush=True)
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
