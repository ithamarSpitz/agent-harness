from __future__ import annotations

import json
import logging
import os
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .home import harness_home


LOGGER = logging.getLogger(__name__)
_LOCK_TIMEOUT_SECONDS = 10.0
_warning_lock = threading.Lock()
_warning_logged = False
_folder_override: Path | None = None
_folder_configured = False


def set_outcomes_folder(folder: Path | None) -> None:
    """Redirect or disable the shared outcomes log (test seam)."""
    global _folder_configured, _folder_override
    _folder_configured = True
    _folder_override = folder


def _resolve_folder() -> Path | None:
    if _folder_configured:
        return _folder_override
    return harness_home()


def append_outcome(record: dict[str, Any]) -> None:
    """Append one outcome without allowing shared-log failures to escape."""
    try:
        folder = _resolve_folder()
        if folder is None:
            return
        folder.mkdir(parents=True, exist_ok=True)
        line = (json.dumps(record, sort_keys=True) + "\n").encode("utf-8")
        with _locked(folder / "outcomes.lock"):
            with (folder / "outcomes.jsonl").open("ab") as outcome_file:
                outcome_file.write(line)
                outcome_file.flush()
    except Exception as exc:
        _warn_once(exc)


def _warn_once(exc: Exception) -> None:
    global _warning_logged
    with _warning_lock:
        if _warning_logged:
            return
        _warning_logged = True
    LOGGER.warning("Shared outcomes log unavailable; continuing without it: %s", exc)


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    with path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                _try_lock(lock_file)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"timed out waiting for {path}")
                time.sleep(0.05)
        try:
            yield
        finally:
            _unlock(lock_file)


def _try_lock(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl: Any = __import__("fcntl")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(lock_file: BinaryIO) -> None:
    if os.name == "nt":
        import msvcrt

        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl: Any = __import__("fcntl")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
