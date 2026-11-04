"""Live board refresh loop for run/resume/watch."""

from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import _thread
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, TextIO, Literal

from .board import (
    BoardEvent,
    BoardSnapshot,
    live_headroom_source,
    render_board,
    snapshot_from_state,
)
from .console_commands import ConsoleContext, QuitSignal, capture_cli, run_command
from .console_input import KeyReader, LineEditor
from .console_screen import PROMPT_PREFIX, compose
from .events import events_path, read_events
from .memory import free_memory_gb as default_free_memory_gb
from .models import SprintSpec, TaskStatus
from .terminal import Screen, color_for, enable_ansi, supports_unicode

_AGENT_STATUSES = frozenset(
    {
        TaskStatus.IMPLEMENTING,
        TaskStatus.CHECKING,
        TaskStatus.REVIEWING,
    }
)
_RESET = "\x1b[0m"
_DEFAULT_INTERVAL = 2.0
_POLL_INTERVAL = 0.2

ConsoleMode = Literal["watch", "run"]


def _aware_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_event_time(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _format_gb(value: float) -> str:
    rounded = round(value, 1)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.1f}"


def format_parallel_summary(
    *,
    running: int,
    maximum: int,
    free_gb: float | None = None,
    per_agent_gb: float | None = None,
) -> str:
    line = f"parallel {running}/{maximum}"
    if free_gb is not None and per_agent_gb is not None:
        line += (
            f" (free {_format_gb(free_gb)} GB,"
            f" ~{_format_gb(per_agent_gb)} GB/agent)"
        )
    return line


def count_running_agents(state: Any, sprint: SprintSpec) -> int:
    total = 0
    for task in sprint.tasks.values():
        if state.get(task.id).status in _AGENT_STATUSES:
            total += 1
    return total


def memory_estimate_gb(state: Any, config: Any) -> float:
    stored = state.get_meta("resources.memory_per_agent_gb")
    try:
        if isinstance(stored, (int, float, str)):
            return float(stored)
    except (TypeError, ValueError):
        pass
    execution = getattr(config, "execution", config)
    return float(getattr(execution, "memory_per_agent_gb", 1.5))


def sprint_has_active_run(state: Any, sprint_id: str) -> bool:
    run_id = state.get_meta(f"{sprint_id}.active_run_id")
    return isinstance(run_id, str) and bool(run_id.strip())


def find_active_sprint_id(roadmap: Any, state: Any) -> str | None:
    for sprint_id in roadmap.sprints:
        if sprint_has_active_run(state, sprint_id):
            return sprint_id
    return None


def sprint_run_finished(state: Any, sprint: SprintSpec) -> bool:
    if sprint_has_active_run(state, sprint.id):
        return False
    status = state.get_meta(f"{sprint.id}.run_status")
    if status == "COMPLETED":
        return True
    tasks = list(sprint.tasks.values())
    if not tasks:
        return False
    return all(state.get(task.id).status == TaskStatus.DONE for task in tasks)


def board_events(root: Path, sprint_id: str, *, limit: int = 5) -> list[BoardEvent]:
    events: list[BoardEvent] = []
    for item in read_events(root, sprint_id, limit):
        when = _parse_event_time(item.get("time"))
        text = str(item.get("text") or "").strip()
        if when is None or not text:
            continue
        events.append(BoardEvent(when, text))
    return events


def should_use_board(
    stream: TextIO | None = None,
    *,
    no_board: bool = False,
    ansi_enabled: Callable[[], bool] | None = None,
) -> bool:
    if no_board:
        return False
    target = sys.stdout if stream is None else stream
    isatty = getattr(target, "isatty", None)
    if not callable(isatty) or not isatty():
        return False
    checker = enable_ansi if ansi_enabled is None else ansi_enabled
    return bool(checker())


def should_use_console(
    stdout: TextIO | None = None,
    stdin: TextIO | None = None,
    *,
    no_board: bool = False,
    ansi_enabled: Callable[[], bool] | None = None,
) -> bool:
    """True when the live board can also host the interactive console prompt."""
    if not should_use_board(stdout, no_board=no_board, ansi_enabled=ansi_enabled):
        return False
    source = sys.stdin if stdin is None else stdin
    isatty = getattr(source, "isatty", None)
    return bool(callable(isatty) and isatty())


def colorize_board_lines(
    snapshot: BoardSnapshot,
    lines: list[str],
    *,
    environment: dict[str, str] | None = None,
) -> list[str]:
    env = os.environ if environment is None else environment
    if "NO_COLOR" in env:
        return list(lines)
    status_by_id = {
        task.id: task.status
        for stage in snapshot.stages
        for task in stage.tasks
    }
    colored: list[str] = []
    for line in lines:
        stripped = line.lstrip()
        indent = line[: len(line) - len(stripped)]
        matched = False
        for task_id, status in sorted(
            status_by_id.items(), key=lambda item: len(item[0]), reverse=True
        ):
            if not stripped.startswith(f"{task_id} {status}"):
                continue
            color = color_for(status, env)
            if color:
                after = stripped[len(task_id) + 1 + len(status) :]
                colored.append(f"{indent}{task_id} {color}{status}{_RESET}{after}")
            else:
                colored.append(line)
            matched = True
            break
        if not matched:
            colored.append(line)
    return colored


@contextmanager
def suppress_progress_stdout(_real_stdout: TextIO) -> Iterator[None]:
    """Hide scheduler progress prints while the board owns the real stdout stream."""
    sink = io.StringIO()
    previous = sys.stdout
    sys.stdout = sink
    try:
        yield
    finally:
        sys.stdout = previous


class BoardLoop:
    """Build snapshots and redraw a Screen on an interval or state change."""

    def __init__(
        self,
        runtime: Any,
        sprint_id: str,
        *,
        stream: TextIO | None = None,
        interval: float = _DEFAULT_INTERVAL,
        poll_interval: float = _POLL_INTERVAL,
        reload_from_disk: bool = False,
        stop_when_finished: bool = False,
        free_memory: Callable[[], float | None] | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        enable_ansi_fn: Callable[[], bool] | None = None,
        environment: dict[str, str] | None = None,
        interactive: bool = False,
        mode: ConsoleMode = "watch",
        key_reader_factory: Callable[[], KeyReader] | None = None,
        editor: LineEditor | None = None,
        headroom_source: Callable[[], Any] | None = live_headroom_source,
    ) -> None:
        self.runtime = runtime
        self.sprint_id = sprint_id
        self.stream = sys.stdout if stream is None else stream
        self.interval = max(0.01, float(interval))
        self.poll_interval = max(0.01, float(poll_interval))
        self.reload_from_disk = reload_from_disk
        self.stop_when_finished = stop_when_finished
        self.interactive = bool(interactive)
        self.mode: ConsoleMode = mode
        self._free_memory = free_memory or default_free_memory_gb
        self._now = now or _aware_now
        self._sleep = sleep or time.sleep
        self._enable_ansi = enable_ansi_fn or enable_ansi
        self._environment = environment
        self._key_reader_factory = key_reader_factory
        self._editor = editor if editor is not None else LineEditor()
        self._headroom_source = headroom_source
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._screen = Screen(self.stream)
        self._last_fingerprint: object | None = None
        self._key_reader: KeyReader | None = None
        self._key_buffer: deque[str] = deque()
        self._output_lines: list[str] = []
        self._confirming = False

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def start_background(self) -> "BoardLoop":
        if self._thread is not None and self._thread.is_alive():
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self.run_until_stopped,
            name=f"harness-board-{self.sprint_id}",
            daemon=True,
        )
        self._thread.start()
        return self

    def close(self) -> None:
        self.stop()
        # Restore TTY mode even if the board thread is stuck in a command
        # and join times out — Decision 4 requires every exit path.
        self._restore_key_reader()
        self.join(timeout=self.interval + 1.0)
        self._screen.close()

    def _restore_key_reader(self) -> None:
        reader = self._key_reader
        if reader is None:
            return
        try:
            reader.__exit__(None, None, None)
        except Exception:
            pass

    def _make_key_reader(self) -> KeyReader:
        if self._key_reader_factory is not None:
            return self._key_reader_factory()
        return KeyReader()

    def _console_context(self) -> ConsoleContext:
        return ConsoleContext(
            runtime=self.runtime,
            sprint_id=self.sprint_id,
            confirm=self._confirm,
            call_cli=capture_cli,
            now=self._now(),
        )

    def _drain_keys(self) -> None:
        """Pull newly available keys into the shared buffer."""
        if self._key_reader is None:
            return
        self._key_buffer.extend(self._key_reader.poll())

    def _confirm(self, prompt: str) -> bool:
        """Ask y/N through the key reader; used by console change commands."""
        if self._key_reader is None:
            return False
        previous = list(self._output_lines)
        self._output_lines = previous + [prompt]
        self._confirming = True
        try:
            try:
                self.draw_once()
            except Exception:
                pass
            while not self._stop.is_set():
                self._drain_keys()
                while self._key_buffer:
                    key = self._key_buffer.popleft()
                    if key in {"y", "Y"}:
                        self._output_lines = previous
                        return True
                    if key in {"n", "N", "ENTER", "ESCAPE"}:
                        self._output_lines = previous
                        return False
                    if key == "CTRL_C":
                        # Same path as _poll_input: pause the run on the main
                        # thread (mode=run) instead of killing only this board thread.
                        self._handle_interrupt()
                        return False
                self._sleep(self.poll_interval)
                try:
                    self.draw_once()
                except Exception:
                    pass
            return False
        finally:
            self._confirming = False

    def _write_stop_request(self) -> list[str]:
        now = self._now()
        harness_dir = Path(self.runtime.root) / ".harness"
        harness_dir.mkdir(parents=True, exist_ok=True)
        path = harness_dir / "stop-request"
        payload = {"sprint": self.sprint_id, "time": now.isoformat()}
        path.write_text(json.dumps(payload) + "\n", encoding="utf-8")
        return [f"Stop requested for {self.sprint_id}."]

    def _handle_quit(self) -> None:
        if self.mode == "watch":
            self.stop()
            return
        if self._confirm(f"Stop sprint {self.sprint_id}? [y/N]"):
            self._output_lines = self._write_stop_request()

    def _handle_line(self, line: str) -> None:
        try:
            output = run_command(line, self._console_context())
        except QuitSignal:
            self._handle_quit()
            return
        except Exception as exc:
            self._output_lines = [f"Error: {exc}"]
            return
        self._output_lines = list(output)

    def _handle_interrupt(self) -> None:
        self.stop()
        if self.mode == "watch":
            raise KeyboardInterrupt
        _thread.interrupt_main()

    def _poll_input(self) -> bool:
        """Feed available keys into the editor. Returns True if a redraw is needed."""
        if not self.interactive or self._key_reader is None or self._confirming:
            return False
        self._drain_keys()
        if not self._key_buffer:
            return False
        while self._key_buffer:
            key = self._key_buffer.popleft()
            try:
                completed = self._editor.feed(key)
            except KeyboardInterrupt:
                self._handle_interrupt()
                return True
            if completed is not None:
                # Confirm may consume leftover keys already in this buffer
                # (e.g. quit+Enter+y in one poll / paste burst).
                self._handle_line(completed)
                if self._stop.is_set():
                    break
        return True

    def _merge_added(self, sprint: SprintSpec) -> None:
        scheduler = getattr(self.runtime, "scheduler", None)
        queue = getattr(scheduler, "task_queue", None) if scheduler is not None else None
        if queue is not None and hasattr(queue, "merge_added_tasks_from_state"):
            queue.merge_added_tasks_from_state(sprint)

    def _sprint(self) -> SprintSpec:
        """Return a private sprint view for the board thread.

        Never mutate the roadmap/scheduler live ``sprint.tasks`` map: the
        scheduler iterates and updates it on other threads during run/resume.
        """
        live = self.runtime.roadmap.sprint(self.sprint_id)
        sprint = SprintSpec(
            id=live.id,
            name=live.name,
            tasks=dict(live.tasks),
            stage_verification=dict(live.stage_verification),
        )
        self._merge_added(sprint)
        return sprint

    def _maybe_reload(self) -> None:
        if not self.reload_from_disk:
            return
        state = self.runtime.state
        if hasattr(state, "load"):
            state.load()

    def _fingerprint(self, sprint: SprintSpec) -> object:
        state = self.runtime.state
        task_parts = tuple(
            (
                task.id,
                state.get(task.id).status.value,
                state.get(task.id).attempt,
                bool(state.get(task.id).added),
            )
            for task in sorted(sprint.tasks.values(), key=lambda item: item.id)
        )
        meta_parts = (
            state.get_meta(f"{self.sprint_id}.run_status"),
            state.get_meta(f"{self.sprint_id}.active_run_id"),
            state.get_meta(f"{self.sprint_id}.current_stage"),
            state.get_meta("resources.memory_per_agent_gb"),
        )
        stamps: list[int] = []
        for path in (Path(state.path), events_path(Path(self.runtime.root))):
            try:
                stamps.append(path.stat().st_mtime_ns)
            except OSError:
                stamps.append(-1)
        return (task_parts, meta_parts, tuple(stamps))

    def build_snapshot(self) -> BoardSnapshot:
        self._maybe_reload()
        sprint = self._sprint()
        state = self.runtime.state
        config = self.runtime.config
        free_gb = self._free_memory()
        per_agent = memory_estimate_gb(state, config)
        running = count_running_agents(state, sprint)
        maximum = max(1, int(config.execution.max_parallel_agents))
        default_max_attempts = int(config.execution.default_max_attempts)
        providers = getattr(config, "providers", None)
        provider_order = getattr(providers, "priority", None)
        return snapshot_from_state(
            state,
            sprint,
            self._now(),
            events=board_events(Path(self.runtime.root), self.sprint_id, limit=5),
            parallel_running=running,
            parallel_max=maximum,
            free_gb=free_gb,
            per_agent_gb=per_agent,
            default_max_attempts=default_max_attempts,
            provider_order=provider_order,
            headroom_source=self._headroom_source if providers is not None else None,
        )

    def render_lines(self, snapshot: BoardSnapshot | None = None) -> list[str]:
        snap = self.build_snapshot() if snapshot is None else snapshot
        width = self._screen.size().columns
        unicode_ok = supports_unicode(self.stream)
        lines = render_board(snap, width, unicode=unicode_ok)
        return colorize_board_lines(snap, lines, environment=self._environment)

    def draw_once(self) -> BoardSnapshot:
        snapshot = self.build_snapshot()
        board_lines = self.render_lines(snapshot)
        if self.interactive:
            size = self._screen.size()
            prompt_text = f"{PROMPT_PREFIX}{self._editor.text}"
            cursor = len(PROMPT_PREFIX) + self._editor.cursor
            lines = compose(
                board_lines,
                self._output_lines,
                prompt_text,
                cursor,
                size.columns,
                size.lines,
            )
            self._screen.draw(lines)
        else:
            self._screen.draw(board_lines)
        self._last_fingerprint = self._fingerprint(self._sprint())
        return snapshot

    def run_until_stopped(self) -> None:
        self._enable_ansi()
        if self.interactive:
            reader = self._make_key_reader()
            with reader:
                self._key_reader = reader
                try:
                    self._run_frames()
                finally:
                    self._key_reader = None
            return
        self._run_frames()

    def _run_frames(self) -> None:
        next_forced = time.monotonic()
        while not self._stop.is_set():
            self._maybe_reload()
            sprint = self._sprint()
            if self.stop_when_finished and sprint_run_finished(self.runtime.state, sprint):
                try:
                    self.draw_once()
                except Exception:
                    pass
                break
            input_changed = self._poll_input()
            if self._stop.is_set():
                break
            fingerprint = self._fingerprint(sprint)
            now_mono = time.monotonic()
            board_tick = (
                fingerprint != self._last_fingerprint
                or now_mono >= next_forced
                or self._last_fingerprint is None
            )
            if input_changed or board_tick:
                try:
                    self.draw_once()
                except Exception:
                    # Drawing must never kill the run or watch loop.
                    pass
                if board_tick:
                    next_forced = time.monotonic() + self.interval
            self._sleep(min(self.poll_interval, self.interval))
