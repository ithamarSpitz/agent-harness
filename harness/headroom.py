from __future__ import annotations

import errno
import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO, Callable, Iterator

from .agy_usage import AgyBucket, fetch_agy_usage
from .home import harness_home
from .pace import record_samples
from .limits import LimitsPaths, _agy_settings, load_claude, load_codex, load_cursor

if TYPE_CHECKING:
    from .config import HarnessConfig


@dataclass(frozen=True, slots=True)
class Headroom:
    provider: str
    used_percent: float | None
    resets_at: int | None
    window: str
    known: bool


LimitsSource = Callable[[LimitsPaths], tuple[list[str], dict[str, Any]]]
AgyUsageSource = Callable[..., list[AgyBucket]]

_PROVIDER_WINDOWS = {
    "codex": ("five_hour", "weekly"),
    "cursor": ("composer_grok", "other_models"),
    "claude": ("five_hour", "weekly"),
}
_MEMORY_LOCK = threading.Lock()
_memory_cached_at: float | None = None
_memory_cache_path: Path | None = None
_memory_snapshot: dict[str, tuple[Headroom, ...]] | None = None
_memory_agy_enabled: bool | None = None


def _unknown(provider: str, window: str) -> Headroom:
    return Headroom(
        provider=provider,
        used_percent=None,
        resets_at=None,
        window=window,
        known=False,
    )


def _entry(provider: str, window: str, raw: Any, used_key: str) -> Headroom:
    if not isinstance(raw, dict):
        return _unknown(provider, window)
    try:
        used_percent = float(raw[used_key])
        resets_at = int(raw["resets_at"])
    except (KeyError, TypeError, ValueError, OverflowError):
        return _unknown(provider, window)
    if resets_at <= 0:
        return _unknown(provider, window)
    return Headroom(provider, used_percent, resets_at, window, True)


def _codex_entries(payload: dict[str, Any]) -> list[Headroom]:
    if "error" in payload:
        raise ValueError("Codex limits unavailable")
    return [
        _entry("codex", "five_hour", payload.get("primary"), "used_percent"),
        _entry("codex", "weekly", payload.get("secondary"), "used_percent"),
    ]


def _cursor_entries(payload: dict[str, Any]) -> list[Headroom]:
    if "error" in payload:
        raise ValueError("Cursor limits unavailable")
    try:
        resets_at = int(payload["billing_cycle_end_ms"]) // 1000
        composer_used = float(payload["auto_percent_used"])
        other_used = float(payload["api_percent_used"])
        if resets_at <= 0:
            raise ValueError("invalid billing cycle")
    except (KeyError, TypeError, ValueError, OverflowError):
        return [_unknown("cursor", window) for window in _PROVIDER_WINDOWS["cursor"]]
    return [
        Headroom("cursor", composer_used, resets_at, "composer_grok", True),
        Headroom("cursor", other_used, resets_at, "other_models", True),
    ]


def _claude_entries(payload: dict[str, Any]) -> list[Headroom]:
    if "error" in payload:
        raise ValueError("Claude limits unavailable")
    windows = payload.get("windows")
    if not isinstance(windows, dict):
        raise ValueError("Claude windows unavailable")
    return [
        _entry("claude", "five_hour", windows.get("five_hour"), "used_percentage"),
        _entry("claude", "weekly", windows.get("seven_day"), "used_percentage"),
    ]


def _load_provider(
    provider: str,
    source: LimitsSource,
    paths: LimitsPaths,
) -> list[Headroom]:
    try:
        _, payload = source(paths)
        if not isinstance(payload, dict):
            raise TypeError("invalid limits payload")
        if provider == "codex":
            return _codex_entries(payload)
        if provider == "cursor":
            return _cursor_entries(payload)
        return _claude_entries(payload)
    except Exception:
        return [_unknown(provider, window) for window in _PROVIDER_WINDOWS[provider]]


def _agy_entries(source: AgyUsageSource, command: str) -> list[Headroom]:
    try:
        buckets = source(command=command)
        if not buckets:
            raise ValueError("no quota pools returned")
        return [
            Headroom(
                "agy",
                (1.0 - bucket.remaining_fraction) * 100.0,
                int(bucket.reset_time.timestamp()),
                "weekly",
                True,
            )
            for bucket in buckets
        ]
    except Exception:
        return [_unknown("agy", "weekly"), _unknown("agy", "weekly")]


def _copy_snapshot(snapshot: dict[str, tuple[Headroom, ...]]) -> dict[str, list[Headroom]]:
    return {provider: list(entries) for provider, entries in snapshot.items()}


def _freeze(snapshot: dict[str, list[Headroom]]) -> dict[str, tuple[Headroom, ...]]:
    return {provider: tuple(entries) for provider, entries in snapshot.items()}


def _fresh(cached_at: float, now: float, max_age_seconds: float) -> bool:
    age = now - cached_at
    return max_age_seconds > 0 and 0 <= age < max_age_seconds


def _decode_snapshot(
    raw: Any, *, agy_enabled: bool = False
) -> tuple[float, dict[str, tuple[Headroom, ...]]] | None:
    if not isinstance(raw, dict):
        return None
    try:
        cached_at = float(raw["cached_at"])
        raw_providers = raw["providers"]
        if not isinstance(raw_providers, dict):
            return None
        snapshot: dict[str, tuple[Headroom, ...]] = {}
        for provider, expected_windows in _PROVIDER_WINDOWS.items():
            raw_entries = raw_providers[provider]
            if not isinstance(raw_entries, list):
                return None
            entries = tuple(Headroom(**entry) for entry in raw_entries if isinstance(entry, dict))
            if tuple(entry.window for entry in entries) != expected_windows:
                return None
            if any(entry.provider != provider for entry in entries):
                return None
            snapshot[provider] = entries
        if agy_enabled:
            raw_entries = raw_providers["agy"]
            if not isinstance(raw_entries, list) or not raw_entries:
                return None
            entries = tuple(Headroom(**entry) for entry in raw_entries if isinstance(entry, dict))
            if len(entries) != len(raw_entries):
                return None
            if any(entry.provider != "agy" or entry.window != "weekly" for entry in entries):
                return None
            snapshot["agy"] = entries
        elif "agy" in raw_providers:
            return None
        return cached_at, snapshot
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


def _read_disk_cache(
    cache_path: Path, *, agy_enabled: bool = False
) -> tuple[float, dict[str, tuple[Headroom, ...]]] | None:
    try:
        return _decode_snapshot(
            json.loads(cache_path.read_text(encoding="utf-8")),
            agy_enabled=agy_enabled,
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _write_disk_cache(
    cache_path: Path,
    cached_at: float,
    snapshot: dict[str, tuple[Headroom, ...]],
) -> None:
    payload = {
        "cached_at": cached_at,
        "providers": {
            provider: [asdict(entry) for entry in entries]
            for provider, entries in snapshot.items()
        },
    }
    fd, temp_name = tempfile.mkstemp(
        prefix="headroom-",
        suffix=".tmp",
        dir=cache_path.parent,
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as cache_file:
            json.dump(payload, cache_file, indent=2, sort_keys=True)
            cache_file.write("\n")
            cache_file.flush()
            os.fsync(cache_file.fileno())
        os.replace(temp_path, cache_path)
    finally:
        temp_path.unlink(missing_ok=True)


def _try_lock_file(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl: Any = __import__("fcntl")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _lock_file(lock_file: BinaryIO) -> None:
    while True:
        try:
            _try_lock_file(lock_file)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                raise
            time.sleep(0.05)


def _unlock_file(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl: Any = __import__("fcntl")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def _disk_lock(cache_path: Path) -> Iterator[bool]:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        lock_file = cache_path.with_suffix(cache_path.suffix + ".lock").open("a+b")
    except OSError:
        yield False
        return
    with lock_file:
        try:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            _lock_file(lock_file)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            try:
                _unlock_file(lock_file)
            except OSError:
                pass


def headroom_snapshot(
    max_age_seconds: float = 300,
    *,
    codex_source: LimitsSource = load_codex,
    cursor_source: LimitsSource = load_cursor,
    claude_source: LimitsSource = load_claude,
    paths: LimitsPaths | None = None,
    cache_path: Path | None = None,
    now: Callable[[], float] | None = None,
    history_path: Path | None = None,
    agy_enabled: bool | None = None,
    agy_command: str | None = None,
    agy_source: AgyUsageSource = fetch_agy_usage,
    config: HarnessConfig | None = None,
    root: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, list[Headroom]]:
    """Return a cheap, recent view of provider usage and reset times.

    Sources and paths are injectable so callers and tests can avoid live provider
    access. Source failures are represented by unknown entries and never escape.
    """

    global _memory_agy_enabled, _memory_cache_path, _memory_cached_at, _memory_snapshot

    now = now or time.time
    cache_path = (cache_path or harness_home() / "headroom-cache.json").expanduser()
    if agy_enabled is None or agy_command is None:
        configured_enabled, configured_command = _agy_settings(
            config, root=root, config_path=config_path
        )
        if agy_enabled is None:
            agy_enabled = configured_enabled
        if agy_command is None:
            agy_command = configured_command

    with _MEMORY_LOCK:
        current_time = now()
        if (
            _memory_snapshot is not None
            and _memory_cached_at is not None
            and _memory_cache_path == cache_path
            and _memory_agy_enabled == agy_enabled
            and _fresh(_memory_cached_at, current_time, max_age_seconds)
        ):
            return _copy_snapshot(_memory_snapshot)

        with _disk_lock(cache_path) as cache_available:
            current_time = now()
            if cache_available:
                disk_cache = _read_disk_cache(cache_path, agy_enabled=agy_enabled)
                if disk_cache is not None and _fresh(
                    disk_cache[0], current_time, max_age_seconds
                ):
                    _memory_cached_at, _memory_snapshot = disk_cache
                    _memory_cache_path = cache_path
                    _memory_agy_enabled = agy_enabled
                    return _copy_snapshot(_memory_snapshot)

            try:
                source_paths = paths or LimitsPaths.from_env()
            except Exception:
                snapshot = {
                    provider: [_unknown(provider, window) for window in windows]
                    for provider, windows in _PROVIDER_WINDOWS.items()
                }
            else:
                snapshot = {
                    "codex": _load_provider("codex", codex_source, source_paths),
                    "cursor": _load_provider("cursor", cursor_source, source_paths),
                    "claude": _load_provider("claude", claude_source, source_paths),
                }
            if agy_enabled:
                snapshot["agy"] = _agy_entries(agy_source, agy_command)
            frozen = _freeze(snapshot)
            _memory_cached_at = current_time
            _memory_cache_path = cache_path
            _memory_agy_enabled = agy_enabled
            _memory_snapshot = frozen
            record_samples(
                [entry for entries in frozen.values() for entry in entries],
                now=current_time,
                path=history_path,
            )
            if cache_available:
                try:
                    _write_disk_cache(cache_path, current_time, frozen)
                except OSError:
                    pass
            return _copy_snapshot(frozen)
