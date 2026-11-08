from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .brief_readthrough import readthrough_prompt
from .command_locate import locate
from .config import HarnessConfig
from .context import ContextResolver
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


COPILOT_QUOTA_PATTERNS = (
    "premium request quota",
    "premium requests quota",
    "quota exhausted",
    "out of premium requests",
)
COPILOT_AUTH_PATTERNS = (
    "not signed in",
    "authentication required",
    "please sign in",
)
COPILOT_NOT_FOUND = (
    "copilot not found on PATH (installed after this shell started? restart the shell "
    "or set copilot.command to the full path)"
)
_IMPLEMENT_DENY_RULES = (
    "shell(git push)",
    "shell(git commit)",
    "shell(git reset)",
    "shell(git checkout)",
    "shell(git rebase)",
    "shell(gh)",
)
_READ_ONLY_ALLOW_RULES = (
    "shell(git diff:*)",
    "shell(git log:*)",
    "shell(git show:*)",
    "shell(git status:*)",
)
_PROMPT_FILE_THRESHOLD = 24_000


class CopilotAgentRunner:
    """Run implementation and analysis agents through GitHub Copilot CLI."""

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
        command = self.config.copilot.command
        executable = command if os.path.isabs(command) else locate(command)
        if executable is None:
            self._availability = (False, COPILOT_NOT_FOUND)
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
                part.strip() for part in (completed.stdout, completed.stderr) if part.strip()
            )
            ok = completed.returncode == 0
            self._availability = (
                ok,
                output or ("available" if ok else "copilot --version failed"),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._availability = (False, str(exc))
        return self._availability

    def _implementation_profile(
        self, attempt: int, *, model_class: str | None = None
    ) -> tuple[str, str | None, str | None]:
        if (model_class or "").strip().casefold() == "escalation":
            return (
                self.config.copilot.escalation_model,
                self.config.copilot.escalation_auto_tier,
                self.config.copilot.escalation_effort,
            )
        sequence = self.config.copilot.implementation_sequence
        index = min(max(0, int(attempt) - 1), len(sequence) - 1)
        profile = sequence[index]
        auto_tier = profile.get("auto_tier")
        effort = profile.get("effort")
        return (
            str(profile["model"]),
            str(auto_tier) if auto_tier is not None else None,
            str(effort) if effort is not None else None,
        )

    def _review_profile(self) -> tuple[str, str | None, str | None]:
        return (
            self.config.copilot.review_model,
            self.config.copilot.review_auto_tier,
            self.config.copilot.review_effort,
        )

    @staticmethod
    def _matches(output: str, patterns: Iterable[str]) -> bool:
        lowered = output.replace("’", "'").replace("‘", "'").casefold()
        return any(pattern.casefold() in lowered for pattern in patterns)

    @staticmethod
    def _parse_output(output: str) -> tuple[str, str | None, int, bool]:
        answer = ""
        routed_model: str | None = None
        premium_requests = 0
        result_ok = False
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            event_type = event.get("type")
            data = event.get("data")
            if event_type == "assistant.message" and isinstance(data, dict):
                content = data.get("content")
                if isinstance(content, str):
                    answer = content
            elif event_type == "model.call_final_result" and isinstance(data, dict):
                model = data.get("model")
                if isinstance(model, str):
                    routed_model = model
            elif event_type == "result":
                result_ok = event.get("exitCode") == 0
                usage = event.get("usage")
                if isinstance(usage, dict):
                    value = usage.get("premiumRequests")
                    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                        premium_requests = value
        return answer, routed_model, premium_requests, result_ok

    def _command(
        self,
        executable: str,
        prompt: str,
        workspace: Path,
        model: str,
        effort: str | None,
        *,
        read_only: bool,
        prompt_dir: Path | None = None,
    ) -> list[str]:
        command = [
            executable,
            "-p",
            prompt,
            "--output-format",
            "json",
            "-C",
            str(workspace),
            "--no-auto-update",
            "--disable-builtin-mcps",
            "--model",
            model,
        ]
        if effort is not None:
            command += ["--reasoning-effort", effort]
        if prompt_dir is not None:
            command += ["--add-dir", str(prompt_dir)]
        if read_only:
            command += ["--deny-tool", "write"]
            for rule in _READ_ONLY_ALLOW_RULES:
                command += ["--allow-tool", rule]
        else:
            command.append("--allow-all-tools")
            for rule in _IMPLEMENT_DENY_RULES:
                command += ["--deny-tool", rule]
        return command

    def _prepare_prompt(self, prompt: str, log_name: str) -> tuple[str, Path | None]:
        if len(prompt) <= _PROMPT_FILE_THRESHOLD:
            return prompt, None
        self.log_dir.mkdir(parents=True, exist_ok=True)
        prompt_path = self.log_dir / f"{Path(log_name).stem}-prompt.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        return (
            f"Read the complete prompt from {prompt_path} and follow it exactly.",
            self.log_dir,
        )

    def _record_usage(
        self,
        task_id: str,
        phase: str,
        requested_model: str,
        routed_model: str | None,
        auto_tier: str | None,
        effort: str | None,
        premium_requests: int,
        attempt: int | None,
        result: AgentResult,
        *,
        sprint: str | None = None,
    ) -> None:
        if self.usage is None:
            return
        event: dict[str, object] = {
            "provider": "copilot",
            "task_id": task_id,
            "phase": phase,
            "attempt": attempt,
            "sprint": sprint,
            "model_class": "copilot",
            "model": routed_model or requested_model,
            "requested_model": requested_model,
            "auto_tier": auto_tier,
            "effort": effort,
            "premium_requests": premium_requests,
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

    def _invoke(
        self,
        *,
        task_id: str,
        phase: str,
        prompt: str,
        workspace: Path,
        model: str,
        auto_tier: str | None,
        effort: str | None,
        timeout_minutes: int,
        log_name: str,
        read_only: bool,
        attempt: int | None = None,
        env: dict[str, str] | None = None,
        sprint: str | None = None,
        on_process_start: Callable[[int], None] | None = None,
    ) -> AgentResult:
        configured_command = self.config.copilot.command
        executable = (
            configured_command
            if os.path.isabs(configured_command)
            else locate(configured_command)
        )
        if executable is None:
            result = AgentResult(
                False,
                "",
                provider_could_not_start("copilot", COPILOT_NOT_FOUND),
                model=model,
                provider="copilot",
                launch_failure=True,
            )
            write_log(self.log_dir, log_name, COPILOT_NOT_FOUND)
            self._record_usage(
                task_id, phase, model, None, auto_tier, effort, 0, attempt, result,
                sprint=sprint,
            )
            return result

        effective_prompt, prompt_dir = self._prepare_prompt(prompt, log_name)
        command = self._command(
            executable,
            effective_prompt,
            workspace,
            model,
            effort,
            read_only=read_only,
            prompt_dir=prompt_dir,
        )
        child_env = dict(os.environ if env is None else env)
        if auto_tier is not None:
            child_env["COPILOT_AUTO_TIER"] = auto_tier
        else:
            # A parent shell may retain a tier from a manual Copilot session.
            # Profiles without a tier must not inherit that per-call setting.
            child_env.pop("COPILOT_AUTO_TIER", None)

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
        except OSError as exc:
            result = AgentResult(
                False,
                "",
                provider_could_not_start("copilot", str(exc)),
                duration_seconds=time.monotonic() - started,
                model=model,
                provider="copilot",
                launch_failure=True,
            )
            write_log(self.log_dir, log_name, str(exc))
            self._record_usage(
                task_id, phase, model, None, auto_tier, effort, 0, attempt, result,
                sprint=sprint,
            )
            return result

        def read_output() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                with output_lock:
                    output_lines.append(line)
                    last_output_at[0] = time.monotonic()

        reader = threading.Thread(
            target=read_output,
            name=f"{task_id}-{phase}-copilot-stdout",
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
                error = f"Copilot timed out after {timeout_minutes} minutes"
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
                    "Copilot stalled: no CLI output or repository file activity for "
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
        write_log(
            self.log_dir,
            log_name,
            raw_output + (("\n" + error) if error else ""),
        )

        if return_code is not None and int(return_code) in WINDOWS_RESOURCE_EXIT_CODES:
            result = AgentResult(
                False,
                "",
                resource_could_not_start("copilot", int(return_code)),
                duration_seconds=duration,
                model=model,
                provider="copilot",
                transient_error=True,
                resource_launch_failure=True,
            )
            self._record_usage(
                task_id, phase, model, None, auto_tier, effort, 0, attempt, result,
                sprint=sprint,
            )
            return result

        answer, routed_model, premium_requests, result_event_ok = self._parse_output(
            raw_output
        )
        call_failed = (
            return_code != 0 or timed_out or stalled or not result_event_ok
        )
        quota_exhausted = call_failed and self._matches(
            raw_output, COPILOT_QUOTA_PATTERNS
        )
        auth_failure = (
            call_failed
            and not quota_exhausted
            and not timed_out
            and not stalled
            and self._matches(raw_output, COPILOT_AUTH_PATTERNS)
        )
        ok = not call_failed and not quota_exhausted and not auth_failure
        if quota_exhausted:
            error = "Copilot usage quota exhausted"
        elif auth_failure:
            error = "Copilot authentication unavailable"
        elif not result_event_ok and not timed_out and not stalled:
            error = "Copilot JSON stream contained no successful result event"
        elif return_code != 0 and not timed_out and not stalled:
            error = f"Copilot exit code {return_code}"

        result = AgentResult(
            ok=ok,
            output=answer,
            error=error,
            timed_out=timed_out,
            stalled=stalled,
            duration_seconds=duration,
            model=routed_model or model,
            quota_exhausted=quota_exhausted,
            quota_scope="premium_requests" if quota_exhausted else None,
            transient_error=timed_out or stalled,
            waiting_cause="timeout" if timed_out or stalled else None,
            auth_failure=auth_failure,
            provider="copilot",
        )
        self._record_usage(
            task_id,
            phase,
            model,
            routed_model,
            auto_tier,
            effort,
            premium_requests,
            attempt,
            result,
            sprint=sprint,
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
        model, auto_tier, effort = self._implementation_profile(
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
            auto_tier=auto_tier,
            effort=effort,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-attempt-{attempt}-copilot.log",
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
        model, auto_tier, effort = self._review_profile()
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
            model=model,
            auto_tier=auto_tier,
            effort=effort,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-review-copilot.log",
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
        if model_class.strip().casefold() == "escalation":
            model, auto_tier, effort = (
                self.config.copilot.escalation_model,
                self.config.copilot.escalation_auto_tier,
                self.config.copilot.escalation_effort,
            )
        else:
            model, auto_tier, effort = self._review_profile()
        return self._invoke(
            task_id=task.id,
            phase="plan_repair",
            prompt=plan_change_prompt(
                task, failure_summary, self._context_paths(task)
            ),
            workspace=workspace,
            model=model,
            auto_tier=auto_tier,
            effort=effort,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-plan-repair-copilot.log",
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
        model, auto_tier, effort = self._review_profile()
        return self._invoke(
            task_id=task.id,
            phase="brief_check",
            prompt=readthrough_prompt(task, brief_text),
            workspace=workspace,
            model=model,
            auto_tier=auto_tier,
            effort=effort,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-brief-check-copilot.log",
            read_only=True,
            env=env,
            sprint=task.sprint,
        )
