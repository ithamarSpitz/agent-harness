from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .config import HarnessConfig
from .git_worktree import WorktreeManager
from .logging_utils import write_log
from .models import TaskSpec
from .process_utils import stop_process_tree

LOGGER = logging.getLogger(__name__)
ProcessStartHook = Callable[[int], None]

# Identity of the real subprocess.run at import time. Unit tests that replace
# subprocess.run (to assert encoding kwargs) are honoured via this check so
# those assertions keep working while production always uses Popen + tree kill.
_SUBPROCESS_RUN = subprocess.run


@dataclass(slots=True)
class VerificationResult:
    ok: bool
    summary: str
    failures: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)


@dataclass(slots=True)
class _CommandOutcome:
    returncode: int
    output: str
    timed_out: bool
    timeout_minutes: float


def _matches(path: str, pattern: str) -> bool:
    normalized = pattern.rstrip("/")
    if normalized.endswith("/**"):
        prefix = normalized[:-3].rstrip("/")
        return path == prefix or path.startswith(prefix + "/")
    return fnmatch.fnmatch(path, normalized)


def _tail_lines(text: str, count: int = 40) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-count:])


def _timeout_message(timeout_minutes: float, command: str) -> str:
    return f"timed out after {timeout_minutes:g} minutes: {command}"


class Verifier:
    def __init__(self, config: HarnessConfig, log_dir: Path, worktrees: WorktreeManager):
        self.config = config
        self.log_dir = log_dir
        self.worktrees = worktrees

    def _verification_env(self, env: dict[str, str] | None) -> dict[str, str]:
        # When the scheduler passes an explicit task/stage environment it is
        # already complete (EnvironmentManager starts from os.environ). Treat
        # that mapping as authoritative instead of consulting ambient process
        # state a second time; this keeps verification deterministic and makes
        # tests/embedded callers able to supply an isolated environment.
        merged = (
            {str(k): str(v) for k, v in env.items()}
            if env is not None
            else os.environ.copy()
        )
        # Verification fallbacks fill only missing values. A real task/runtime
        # environment always wins, so static validation helpers cannot replace
        # actual Docker/database credentials.
        for key, value in self.config.verification.env.items():
            merged.setdefault(str(key), str(value))
        return merged

    def _verification_timeout_seconds(self) -> float:
        return max(0.01, float(self.config.execution.verification_timeout_minutes)) * 60.0

    def _run_timed_command(
        self,
        command: str,
        workspace: Path,
        env: dict[str, str],
        timeout_seconds: float,
        on_process_start: ProcessStartHook | None = None,
    ) -> _CommandOutcome:
        """Run one shell command; on timeout kill the whole process tree.

        Output is collected on a background reader so a hang still yields the
        last lines printed before the limit. Windows uses taskkill /T; POSIX
        starts a new session so the group can be signalled together.
        """
        timeout_minutes = float(timeout_seconds) / 60.0

        # Legacy unit tests replace subprocess.run to assert utf-8 decoding kwargs.
        if subprocess.run is not _SUBPROCESS_RUN:
            try:
                completed = subprocess.run(
                    command,
                    cwd=workspace,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    shell=True,
                    capture_output=True,
                    env=env,
                    timeout=timeout_seconds,
                )
                return _CommandOutcome(
                    int(completed.returncode),
                    f"{completed.stdout}{completed.stderr}",
                    False,
                    timeout_minutes,
                )
            except subprocess.TimeoutExpired as exc:
                stdout = exc.stdout if isinstance(exc.stdout, str) else (
                    exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""
                )
                stderr = exc.stderr if isinstance(exc.stderr, str) else (
                    exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""
                )
                return _CommandOutcome(
                    -1,
                    stdout + (f"\n{stderr}" if stderr else ""),
                    True,
                    timeout_minutes,
                )

        output_chunks: list[str] = []
        output_lock = threading.Lock()
        proc: subprocess.Popen[str] = subprocess.Popen(
            command,
            cwd=workspace,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            # Own process group so stop_process_tree never reaches the harness.
            start_new_session=(os.name != "nt"),
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
            ),
        )
        if on_process_start is not None:
            try:
                on_process_start(proc.pid)
            except Exception as exc:
                LOGGER.warning(
                    "on_process_start callback failed for verification process %s: %s",
                    proc.pid,
                    exc,
                )

        def read_output() -> None:
            assert proc.stdout is not None
            while True:
                chunk = proc.stdout.read(4096)
                if not chunk:
                    break
                with output_lock:
                    output_chunks.append(chunk)

        reader = threading.Thread(
            target=read_output, name="verification-stdout", daemon=True
        )
        reader.start()

        deadline = time.monotonic() + max(0.01, float(timeout_seconds))
        timed_out = False
        while proc.poll() is None:
            if time.monotonic() >= deadline:
                timed_out = True
                stop_process_tree(proc)
                break
            try:
                proc.wait(timeout=0.1)
            except subprocess.TimeoutExpired:
                pass

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stop_process_tree(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        reader.join(timeout=2)

        with output_lock:
            output = "".join(output_chunks)
        returncode = -1 if timed_out else int(proc.returncode if proc.returncode is not None else -1)
        return _CommandOutcome(returncode, output, timed_out, timeout_minutes)

    def _render_command(self, command: str, outcome: _CommandOutcome) -> str:
        if outcome.timed_out:
            message = _timeout_message(outcome.timeout_minutes, command)
            tail = _tail_lines(outcome.output)
            body = f"{message}\n{tail}".rstrip()
            return f"$ {command}\n{body}".rstrip()
        return (
            f"$ {command}\nexit={outcome.returncode}\n{outcome.output}"
        ).rstrip()

    def requires_review(self, changed_files: list[str]) -> bool:
        return any(
            _matches(path, pattern)
            for path in changed_files
            for pattern in self.config.paths.review_required
        )

    def verify_task(
        self,
        task: TaskSpec,
        workspace: Path,
        base_ref: str,
        *,
        env: dict[str, str] | None = None,
        on_process_start: ProcessStartHook | None = None,
    ) -> VerificationResult:
        failures: list[str] = []
        changed = self.worktrees.changed_files(workspace, base_ref)

        if task.allowed_paths:
            out_of_scope = [
                path for path in changed
                if not any(_matches(path, pattern) for pattern in task.allowed_paths)
            ]
            if out_of_scope:
                failures.append("Changed files outside allowed_paths: " + ", ".join(out_of_scope))

        if not task.allow_protected:
            protected = [
                path for path in changed
                if any(_matches(path, pattern) for pattern in self.config.paths.protected)
            ]
            if protected:
                failures.append("Protected files changed: " + ", ".join(protected))

        command_outputs: list[str] = []
        commands = [*self.config.verification.global_task_commands, *task.verification]
        command_env = self._verification_env(env)
        timeout_seconds = self._verification_timeout_seconds()
        for command in commands:
            outcome = self._run_timed_command(
                command,
                workspace,
                command_env,
                timeout_seconds,
                on_process_start=on_process_start,
            )
            command_outputs.append(self._render_command(command, outcome))
            if outcome.timed_out:
                failures.append(_timeout_message(outcome.timeout_minutes, command))
            elif outcome.returncode != 0:
                failures.append(f"Verification command failed: {command}")

        summary = "\n\n".join([
            "Changed files:\n" + ("\n".join(changed) if changed else "(none)"),
            *command_outputs,
            "Failures:\n" + ("\n".join(failures) if failures else "(none)"),
        ])
        write_log(self.log_dir, f"{task.id}-verification.log", summary)
        return VerificationResult(not failures, summary, failures, changed)

    def verify_integrated_task(
        self,
        task: TaskSpec,
        workspace: Path,
        *,
        env: dict[str, str] | None = None,
        on_process_start: ProcessStartHook | None = None,
    ) -> VerificationResult:
        """Re-run a DONE task's behavioral verification on the integrated checkout.

        This deliberately skips diff/scope checks because the task is already merged;
        the purpose is regression validation after an approved plan revision.
        """
        failures: list[str] = []
        outputs: list[str] = []
        command_env = self._verification_env(env)
        timeout_seconds = self._verification_timeout_seconds()
        for command in [*self.config.verification.global_task_commands, *task.verification]:
            outcome = self._run_timed_command(
                command,
                workspace,
                command_env,
                timeout_seconds,
                on_process_start=on_process_start,
            )
            outputs.append(self._render_command(command, outcome))
            if outcome.timed_out:
                failures.append(_timeout_message(outcome.timeout_minutes, command))
            elif outcome.returncode != 0:
                failures.append(f"Revalidation command failed: {command}")
        summary = "\n\n".join([*outputs, "Failures:\n" + ("\n".join(failures) if failures else "(none)")])
        write_log(self.log_dir, f"{task.id}-revalidation.log", summary)
        return VerificationResult(not failures, summary, failures)

    def run_shell_commands(
        self,
        commands: list[str],
        workspace: Path,
        *,
        env: dict[str, str] | None = None,
        timeout_seconds: int | None = None,
        log_name: str,
        on_process_start: ProcessStartHook | None = None,
    ) -> tuple[bool, str, str | None, int | str | None]:
        outputs: list[str] = []
        command_env = self._verification_env(env)
        effective_timeout = (
            float(timeout_seconds)
            if timeout_seconds is not None
            else self._verification_timeout_seconds()
        )
        for command in commands:
            outcome = self._run_timed_command(
                command,
                workspace,
                command_env,
                effective_timeout,
                on_process_start=on_process_start,
            )
            outputs.append(self._render_command(command, outcome))
            if outcome.timed_out:
                summary = "\n\n".join(outputs)
                write_log(self.log_dir, log_name, summary)
                return False, summary, command, "timeout"
            if outcome.returncode != 0:
                summary = "\n\n".join(outputs)
                write_log(self.log_dir, log_name, summary)
                return False, summary, command, outcome.returncode
        summary = "\n\n".join(outputs)
        write_log(self.log_dir, log_name, summary)
        return True, summary, None, None

    def verify_stage(
        self,
        workspace: Path,
        commands: list[str],
        stage_name: str,
        *,
        env: dict[str, str] | None = None,
        on_process_start: ProcessStartHook | None = None,
    ) -> VerificationResult:
        failures: list[str] = []
        outputs: list[str] = []
        command_env = self._verification_env(env)
        timeout_seconds = self._verification_timeout_seconds()
        for command in [*self.config.verification.stage_commands, *commands]:
            outcome = self._run_timed_command(
                command,
                workspace,
                command_env,
                timeout_seconds,
                on_process_start=on_process_start,
            )
            outputs.append(self._render_command(command, outcome))
            if outcome.timed_out:
                failures.append(_timeout_message(outcome.timeout_minutes, command))
            elif outcome.returncode != 0:
                failures.append(f"Stage command failed: {command}")
        summary = "\n\n".join([*outputs, "Failures:\n" + ("\n".join(failures) if failures else "(none)")])
        write_log(self.log_dir, f"{stage_name}-verification.log", summary)
        return VerificationResult(not failures, summary, failures)
