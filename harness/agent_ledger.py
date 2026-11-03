from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator, TypeVar

from .home import harness_home


LOGGER = logging.getLogger(__name__)
LOCK_TIMEOUT_SECONDS = 10.0
# Brief Windows file-sharing / antivirus windows after another process's os.replace.
_SHARING_RETRY_SECONDS = 2.0
_SHARING_RETRY_SLEEP_SECONDS = 0.05
_WINDOWS_SHARING_WINERRORS = frozenset({5, 32, 33})

ProcessAlive = Callable[[int], bool]
_T = TypeVar("_T")


def _is_transient_sharing_error(exc: BaseException) -> bool:
    if isinstance(exc, PermissionError):
        return True
    if isinstance(exc, OSError):
        winerror = getattr(exc, "winerror", None)
        if winerror in _WINDOWS_SHARING_WINERRORS:
            return True
    return False


def _call_with_sharing_retries(operation: Callable[[], _T]) -> _T:
    deadline = time.monotonic() + _SHARING_RETRY_SECONDS
    while True:
        try:
            return operation()
        except OSError as exc:
            if not _is_transient_sharing_error(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(_SHARING_RETRY_SLEEP_SECONDS)


@dataclass(frozen=True, slots=True)
class AgentRecord:
    project: str
    task_id: str
    harness_pid: int
    estimate_gb: float
    started_at: str


@dataclass(frozen=True, slots=True)
class WaitingRecord:
    project: str
    harness_pid: int
    ready_tasks: int
    since: str


@dataclass(frozen=True, slots=True)
class Ledger:
    agents: dict[str, AgentRecord]
    waiting: dict[str, WaitingRecord]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _absolute_project(project: str | Path) -> str:
    return str(Path(project).expanduser().resolve())


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        open_process.restype = wintypes.HANDLE
        handle = open_process(process_query_limited_information, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # Access denied means the process exists but cannot be inspected.
        return ctypes.get_last_error() == 5

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        return exc.errno == errno.EPERM
    return True


class LedgerStore:
    """Cross-process store for agents run by every harness on this machine."""

    def __init__(
        self,
        folder: Path | None = None,
        *,
        process_alive: ProcessAlive | None = None,
        lock_timeout: float = LOCK_TIMEOUT_SECONDS,
    ) -> None:
        self.folder = (folder or harness_home()).expanduser()
        self.path = self.folder / "agents.json"
        self.lock_path = self.folder / "agents.lock"
        self.process_alive = process_alive or _process_alive
        self.lock_timeout = lock_timeout

    def register_agent(
        self,
        project: str | Path,
        task_id: str,
        harness_pid: int,
        estimate_gb: float,
    ) -> str:
        agent_id = uuid.uuid4().hex

        def change(ledger: Ledger) -> None:
            ledger.agents[agent_id] = AgentRecord(
                project=_absolute_project(project),
                task_id=task_id,
                harness_pid=harness_pid,
                estimate_gb=estimate_gb,
                started_at=_utc_now(),
            )

        # Empty string when the locked write did not persist (Decision 5: never raise).
        result = self._change(change)
        return agent_id if agent_id in result.agents else ""

    def unregister_agent(self, agent_id: str) -> None:
        self._change(lambda ledger: ledger.agents.pop(agent_id, None))

    def set_waiting(self, project: str | Path, harness_pid: int, ready_tasks: int) -> None:
        project_path = _absolute_project(project)
        record_id = self._waiting_id(project_path, harness_pid)

        def change(ledger: Ledger) -> None:
            if ready_tasks <= 0:
                ledger.waiting.pop(record_id, None)
                return
            previous = ledger.waiting.get(record_id)
            ledger.waiting[record_id] = WaitingRecord(
                project=project_path,
                harness_pid=harness_pid,
                ready_tasks=ready_tasks,
                since=previous.since if previous is not None else _utc_now(),
            )

        self._change(change)

    def snapshot(self) -> Ledger:
        return self._change(lambda _ledger: None)

    @staticmethod
    def _waiting_id(project: str, harness_pid: int) -> str:
        value = f"{project}\0{harness_pid}".encode("utf-8")
        return hashlib.sha256(value).hexdigest()

    def _change(self, change: Callable[[Ledger], object]) -> Ledger:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            with self._locked():
                ledger = self._read()
                self._drop_dead_records(ledger)
                change(ledger)
                self._write(ledger)
                return ledger
        except Exception as exc:
            # Capacity coordination must never prevent the harness itself from running.
            LOGGER.warning("Agent ledger unavailable; continuing with an empty ledger: %s", exc)
            return Ledger(agents={}, waiting={})

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self.lock_path.open("a+b") as lock_file:
            lock_file.seek(0, os.SEEK_END)
            if lock_file.tell() == 0:
                lock_file.write(b"\0")
                lock_file.flush()
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    self._try_lock(lock_file)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"timed out after {self.lock_timeout:g}s waiting for {self.lock_path}"
                        )
                    time.sleep(0.05)
            try:
                yield
            finally:
                self._unlock(lock_file)

    @staticmethod
    def _try_lock(lock_file: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            return
        fcntl: Any = __import__("fcntl")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(lock_file: BinaryIO) -> None:
        if os.name == "nt":
            import msvcrt

            lock_file.seek(0)
            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
            return
        fcntl: Any = __import__("fcntl")
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read(self) -> Ledger:
        if not self.path.exists():
            return Ledger(agents={}, waiting={})
        try:
            # Transient sharing errors raise after retries so _change does not clobber the file.
            text = _call_with_sharing_retries(lambda: self.path.read_text(encoding="utf-8"))
            raw = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError("root must be an object")
            raw_agents = raw.get("agents", {})
            raw_waiting = raw.get("waiting", {})
            if not isinstance(raw_agents, dict) or not isinstance(raw_waiting, dict):
                raise ValueError("agents and waiting must be objects")
            agents = {
                str(record_id): self._agent_from_json(record)
                for record_id, record in raw_agents.items()
            }
            waiting = {
                str(record_id): self._waiting_from_json(record)
                for record_id, record in raw_waiting.items()
            }
            return Ledger(agents=agents, waiting=waiting)
        except (UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
            LOGGER.warning("Agent ledger %s is corrupt; replacing it with an empty ledger: %s", self.path, exc)
            return Ledger(agents={}, waiting={})

    @staticmethod
    def _agent_from_json(value: object) -> AgentRecord:
        if not isinstance(value, dict):
            raise ValueError("agent record must be an object")
        return AgentRecord(
            project=str(value["project"]),
            task_id=str(value["task_id"]),
            harness_pid=int(value["harness_pid"]),
            estimate_gb=float(value["estimate_gb"]),
            started_at=str(value["started_at"]),
        )

    @staticmethod
    def _waiting_from_json(value: object) -> WaitingRecord:
        if not isinstance(value, dict):
            raise ValueError("waiting record must be an object")
        return WaitingRecord(
            project=str(value["project"]),
            harness_pid=int(value["harness_pid"]),
            ready_tasks=int(value["ready_tasks"]),
            since=str(value["since"]),
        )

    def _drop_dead_records(self, ledger: Ledger) -> bool:
        alive: dict[int, bool] = {}

        def is_alive(pid: int) -> bool:
            if pid not in alive:
                try:
                    alive[pid] = self.process_alive(pid)
                except Exception as exc:
                    LOGGER.warning("Could not check agent-ledger process %s; keeping its records: %s", pid, exc)
                    alive[pid] = True
            return alive[pid]

        dead_agents = [record_id for record_id, record in ledger.agents.items() if not is_alive(record.harness_pid)]
        dead_waiting = [
            record_id for record_id, record in ledger.waiting.items() if not is_alive(record.harness_pid)
        ]
        for record_id in dead_agents:
            del ledger.agents[record_id]
        for record_id in dead_waiting:
            del ledger.waiting[record_id]
        return bool(dead_agents or dead_waiting)

    def _write(self, ledger: Ledger) -> None:
        payload = {
            "agents": {record_id: asdict(record) for record_id, record in sorted(ledger.agents.items())},
            "waiting": {record_id: asdict(record) for record_id, record in sorted(ledger.waiting.items())},
        }
        fd, temp_name = tempfile.mkstemp(prefix="agents-", suffix=".tmp", dir=self.folder)
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as temp_file:
                json.dump(payload, temp_file, indent=2, sort_keys=True)
                temp_file.write("\n")
                temp_file.flush()
                os.fsync(temp_file.fileno())
            _call_with_sharing_retries(lambda: os.replace(temp_path, self.path))
        finally:
            temp_path.unlink(missing_ok=True)


def _store(folder: Path | None, process_alive: ProcessAlive | None) -> LedgerStore:
    return LedgerStore(folder, process_alive=process_alive)


def register_agent(
    project: str | Path,
    task_id: str,
    harness_pid: int,
    estimate_gb: float,
    *,
    folder: Path | None = None,
    process_alive: ProcessAlive | None = None,
) -> str:
    return _store(folder, process_alive).register_agent(project, task_id, harness_pid, estimate_gb)


def unregister_agent(
    agent_id: str,
    *,
    folder: Path | None = None,
    process_alive: ProcessAlive | None = None,
) -> None:
    _store(folder, process_alive).unregister_agent(agent_id)


def set_waiting(
    project: str | Path,
    harness_pid: int,
    ready_tasks: int,
    *,
    folder: Path | None = None,
    process_alive: ProcessAlive | None = None,
) -> None:
    _store(folder, process_alive).set_waiting(project, harness_pid, ready_tasks)


def snapshot(
    *,
    folder: Path | None = None,
    process_alive: ProcessAlive | None = None,
) -> Ledger:
    return _store(folder, process_alive).snapshot()
