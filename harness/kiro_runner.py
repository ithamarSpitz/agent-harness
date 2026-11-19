from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .brief_readthrough import readthrough_prompt
from .command_locate import locate
from .config import HarnessConfig
from .context import ContextResolver
from .kiro_stream import KiroFlags, classify_kiro, parse_kiro_stream
from .logging_utils import write_log
from .models import (
    TaskSpec,
    WINDOWS_RESOURCE_EXIT_CODES,
    provider_could_not_start,
    resource_could_not_start,
)
from .process_utils import prepare_external_argv, stop_process_tree
from .prompts import implement_prompt, plan_change_prompt, review_prompt
from .runner import AgentResult, CursorAgentRunner
from .usage import UsageRecorder, parse_review_outcome


KIRO_NOT_FOUND = (
    "kiro-cli not found on PATH (installed after this shell started? restart the shell "
    "or set kiro.command to the full path)"
)


class KiroAgentRunner:
    """Run implementation and analysis agents through Kiro CLI."""

    supports_ui_review = False

    def __init__(
        self,
        config: HarnessConfig,
        log_dir: Path,
        context: ContextResolver | None = None,
        usage: UsageRecorder | None = None,
    ):
        self.config = config
        self.log_dir = log_dir
        self.context = context
        self.usage = usage
        self._availability: tuple[bool, str] | None = None

    def availability(self, *, refresh: bool = False) -> tuple[bool, str]:
        if self._availability is not None and not refresh:
            return self._availability
        command = self.config.kiro.command
        executable = command if os.path.isabs(command) else locate(command)
        if executable is None:
            self._availability = (False, KIRO_NOT_FOUND)
            return self._availability
        try:
            completed = subprocess.run(
                prepare_external_argv([executable, "--version"]),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=15,
            )
            output = "\n".join(
                part.strip()
                for part in (completed.stdout, completed.stderr)
                if part.strip()
            )
            ok = completed.returncode == 0
            self._availability = (
                ok,
                output or ("available" if ok else "kiro-cli --version failed"),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._availability = (False, str(exc))
        return self._availability

    def _implementation_model(
        self, attempt: int, *, model_class: str | None = None
    ) -> str:
        if (model_class or "").strip().casefold() == "escalation":
            return self.config.kiro.escalation_model
        sequence = self.config.kiro.implementation_sequence
        index = min(max(0, int(attempt) - 1), len(sequence) - 1)
        return sequence[index]

    @staticmethod
    def _command(executable: str, model: str, *, read_only: bool) -> list[str]:
        command = [
            executable,
            "chat",
            "--no-interactive",
            "--output-format",
            "stream-json",
            "--model",
            model,
            "--trust-workspace",
        ]
        command.append(
            "--trust-tools=fs_read,execute_bash" if read_only else "--trust-all-tools"
        )
        return command

    def _record_usage(
        self,
        task_id: str,
        phase: str,
        model: str,
        credits: float,
        attempt: int | None,
        result: AgentResult,
        *,
        sprint: str | None = None,
    ) -> None:
        if self.usage is None:
            return
        event: dict[str, object] = {
            "provider": "kiro",
            "task_id": task_id,
            "phase": phase,
            "attempt": attempt,
            "sprint": sprint,
            "model_class": "kiro",
            "model": model,
            "requested_model": model,
            "credits": credits,
            "duration_seconds": round(result.duration_seconds, 3),
            "ok": result.ok,
            "timed_out": result.timed_out,
            "stalled": result.stalled,
            "quota_exhausted": result.quota_exhausted,
            "capacity_exhausted": result.capacity_exhausted,
            "waiting_cause": result.waiting_cause,
            "transient_error": result.transient_error,
            "auth_failure": result.auth_failure,
            "launch_failure": result.launch_failure,
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "output_tokens": 0,
            "reasoning_output_tokens": 0,
            "code_map": False,
            "code_map_calls": 0,
            "output_bytes": len(result.output.encode("utf-8", errors="replace")),
        }
        if phase == "review":
            event["review_verdict"], event["finding_count"] = parse_review_outcome(
                result.ok, result.output
            )
        self.usage.record(event)

    @staticmethod
    def _has_report(output: str, phase: str) -> bool:
        prefix = "STATUS:" if phase == "implement" else "VERDICT:"
        return any(line.strip().startswith(prefix) for line in output.splitlines())

    def _invoke(
        self,
        *,
        task_id: str,
        phase: str,
        prompt: str,
        workspace: Path,
        model: str,
        timeout_minutes: int,
        log_name: str,
        read_only: bool,
        attempt: int | None = None,
        env: dict[str, str] | None = None,
        sprint: str | None = None,
        on_process_start: Callable[[int], None] | None = None,
    ) -> AgentResult:
        configured_command = self.config.kiro.command
        executable = (
            configured_command
            if os.path.isabs(configured_command)
            else locate(configured_command)
        )
        if executable is None:
            result = AgentResult(
                False,
                "",
                provider_could_not_start("kiro", KIRO_NOT_FOUND),
                model=model,
                provider="kiro",
                launch_failure=True,
            )
            write_log(self.log_dir, log_name, KIRO_NOT_FOUND)
            self._record_usage(task_id, phase, model, 0, attempt, result, sprint=sprint)
            return result

        command = self._command(executable, model, read_only=read_only)
        child_env = dict(os.environ if env is None else env)
        started = time.monotonic()
        output_lines: list[str] = []
        output_lock = threading.Lock()
        last_output_at = [started]
        timed_out = False
        stalled = False
        error: str | None = None
        try:
            proc = subprocess.Popen(
                prepare_external_argv(command),
                cwd=workspace,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                env=child_env,
                start_new_session=(os.name != "nt"),
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    if os.name == "nt"
                    else 0
                ),
            )
            if on_process_start is not None:
                on_process_start(proc.pid)
            assert proc.stdin is not None
            try:
                proc.stdin.write(prompt)
            except OSError:
                pass
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        except OSError as exc:
            result = AgentResult(
                False,
                "",
                provider_could_not_start("kiro", str(exc)),
                duration_seconds=time.monotonic() - started,
                model=model,
                provider="kiro",
                launch_failure=True,
            )
            write_log(self.log_dir, log_name, str(exc))
            self._record_usage(task_id, phase, model, 0, attempt, result, sprint=sprint)
            return result

        def read_output() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                with output_lock:
                    output_lines.append(line)
                    last_output_at[0] = time.monotonic()

        reader = threading.Thread(
            target=read_output,
            name=f"{task_id}-{phase}-kiro-stdout",
            daemon=True,
        )
        reader.start()
        hard_timeout = max(0.01, float(timeout_minutes)) * 60.0
        stall_timeout = (
            max(0.0, float(self.config.execution.stall_timeout_minutes)) * 60.0
        )
        poll = max(0.1, float(self.config.execution.watchdog_poll_seconds))
        signature = CursorAgentRunner._workspace_signature(workspace)
        last_repo_activity = started
        while proc.poll() is None:
            now = time.monotonic()
            if now - started >= hard_timeout:
                timed_out = True
                error = f"Kiro timed out after {timeout_minutes} minutes"
                stop_process_tree(proc)
                break
            new_signature = CursorAgentRunner._workspace_signature(workspace)
            if new_signature != signature:
                signature = new_signature
                last_repo_activity = now
            with output_lock:
                output_activity = last_output_at[0]
            if stall_timeout > 0 and now - max(output_activity, last_repo_activity) >= stall_timeout:
                stalled = True
                error = (
                    "Kiro stalled: no CLI output or repository file activity for "
                    f"{self.config.execution.stall_timeout_minutes:g} minutes"
                )
                stop_process_tree(proc)
                break
            try:
                proc.wait(timeout=poll)
            except subprocess.TimeoutExpired:
                pass

        try:
            return_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            stop_process_tree(proc)
            polled_return_code = proc.poll()
            return_code = polled_return_code if polled_return_code is not None else -1
        reader.join(timeout=2)
        duration = time.monotonic() - started
        with output_lock:
            raw_output = "".join(output_lines)

        if return_code is not None and int(return_code) in WINDOWS_RESOURCE_EXIT_CODES:
            write_log(self.log_dir, log_name, raw_output)
            result = AgentResult(
                False,
                "",
                resource_could_not_start("kiro", int(return_code)),
                duration_seconds=duration,
                model=model,
                provider="kiro",
                transient_error=True,
                resource_launch_failure=True,
            )
            self._record_usage(task_id, phase, model, 0, attempt, result, sprint=sprint)
            return result

        stream = parse_kiro_stream(raw_output)
        call_ok = (
            return_code == 0
            and not timed_out
            and not stalled
            and stream.status == "success"
            and stream.error is None
        )
        report_fault = (
            call_ok
            and phase in {"implement", "review"}
            and not self._has_report(stream.final_text, phase)
        )
        if report_fault:
            report = "STATUS" if phase == "implement" else "VERDICT"
            error = (
                f"kiro finished without a {report} report "
                f"({stream.denied_tools} denied tool calls)"
            )

        log_output = raw_output
        if report_fault:
            tail = "\n".join(stream.final_text.splitlines()[-40:])
            log_output = log_output.rstrip() + "\nkiro response tail:\n" + tail + "\n"
        if error:
            log_output = (log_output + "\n" + error).strip() + "\n"
        write_log(self.log_dir, log_name, log_output)

        if timed_out or stalled:
            flags = KiroFlags(
                quota=False,
                auth=False,
                model_unavailable=False,
                capacity=False,
                transient=False,
                config=False,
            )
        else:
            flags = classify_kiro(stream, int(return_code), raw_output)
        waiting_cause: str | None
        if timed_out or stalled:
            waiting_cause = "timeout"
        elif flags.capacity:
            waiting_cause = "provider_busy"
        elif flags.transient:
            waiting_cause = "provider_error"
        else:
            waiting_cause = None

        ok = call_ok and not report_fault
        if flags.quota:
            error = "Kiro usage quota exhausted"
        elif flags.model_unavailable:
            error = f"Kiro model unavailable: {model}"
        elif flags.config:
            error = stream.error or "Kiro configuration error"
        elif flags.capacity:
            error = "Kiro provider capacity temporarily exhausted"
        elif flags.auth:
            error = "Kiro authentication unavailable"
        elif flags.transient and not timed_out and not stalled:
            error = stream.error or "Kiro provider error failure"
        elif not ok and error is None:
            if stream.error is not None:
                error = stream.error
            elif stream.status != "success":
                error = "Kiro stream contained no successful runFinished event"
            elif return_code != 0:
                error = f"Kiro exit code {return_code}"

        result = AgentResult(
            ok=ok,
            output=stream.final_text,
            error=error,
            timed_out=timed_out,
            stalled=stalled,
            duration_seconds=duration,
            model=model,
            quota_exhausted=flags.quota,
            quota_scope="credits" if flags.quota else None,
            capacity_exhausted=flags.capacity,
            transient_error=flags.transient or flags.capacity or timed_out or stalled,
            waiting_cause=waiting_cause,
            auth_failure=flags.auth,
            model_unavailable=flags.model_unavailable,
            config_fault=flags.config,
            launch_failure=report_fault,
            provider="kiro",
        )
        self._record_usage(
            task_id, phase, model, stream.credits, attempt, result, sprint=sprint
        )
        return result

    def _context_paths(self, task: TaskSpec) -> list[str]:
        return self.context.display_paths(task) if self.context is not None else []

    def implement(
        self,
        task: TaskSpec,
        workspace: Path,
        timeout_minutes: int,
        attempt: int,
        previous_failure: str | None,
        *,
        env: dict[str, str] | None = None,
        model_class_override: str | None = None,
        code_map_offered: bool = False,
        on_process_start: Callable[[int], None] | None = None,
        **_kwargs: Any,
    ) -> AgentResult:
        del code_map_offered
        model = self._implementation_model(
            attempt, model_class=model_class_override or task.model_class
        )
        return self._invoke(
            task_id=task.id,
            phase="implement",
            prompt=implement_prompt(
                task,
                previous_failure,
                self._context_paths(task),
                False,
                self.config.code_map.command,
                ui_review=None,
            ),
            workspace=workspace,
            model=model,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-attempt-{attempt}-kiro.log",
            read_only=False,
            attempt=attempt,
            env=env,
            sprint=task.sprint,
            on_process_start=on_process_start,
        )

    def review(
        self,
        task: TaskSpec,
        workspace: Path,
        base_ref: str,
        verification_summary: str,
        timeout_minutes: int,
        *,
        env: dict[str, str] | None = None,
        code_map_offered: bool = False,
        on_process_start: Callable[[int], None] | None = None,
    ) -> AgentResult:
        del code_map_offered
        return self._invoke(
            task_id=task.id,
            phase="review",
            prompt=review_prompt(
                task,
                base_ref,
                verification_summary,
                self._context_paths(task),
                False,
                self.config.code_map.command,
                ui_review=None,
            ),
            workspace=workspace,
            model=self.config.kiro.review_model,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-review-kiro.log",
            read_only=True,
            env=env,
            sprint=task.sprint,
            on_process_start=on_process_start,
        )

    def plan_change(
        self,
        task: TaskSpec,
        workspace: Path,
        failure_summary: str,
        timeout_minutes: int,
        *,
        model_class: str = "reviewer",
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        model = (
            self.config.kiro.escalation_model
            if model_class.strip().casefold() == "escalation"
            else self.config.kiro.review_model
        )
        return self._invoke(
            task_id=task.id,
            phase="plan_repair",
            prompt=plan_change_prompt(task, failure_summary, self._context_paths(task)),
            workspace=workspace,
            model=model,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-plan-repair-kiro.log",
            read_only=True,
            env=env,
            sprint=task.sprint,
        )

    def brief_check(
        self,
        task: TaskSpec,
        workspace: Path,
        brief_text: str,
        timeout_minutes: int,
        *,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        return self._invoke(
            task_id=task.id,
            phase="brief_check",
            prompt=readthrough_prompt(task, brief_text),
            workspace=workspace,
            model=self.config.kiro.review_model,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-brief-check-kiro.log",
            read_only=True,
            env=env,
            sprint=task.sprint,
        )
