from __future__ import annotations

import json
import logging
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from .agy_permissions import build_settings
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


AGY_QUOTA_PATTERNS = (
    "credits balance is too low",
    "credits balance isn't enough",
    "ai: out of credits",
    "quota exhausted",
    "resource_exhausted",
    "out of credits",
)
AGY_CAPACITY_PATTERNS = ("model capacity exhausted", "rate limit")
AGY_NETWORK_PATTERNS = (
    "network error",
    "connection error",
    "connection failed",
    "connection lost",
    "connection reset",
    "connection refused",
    "failed to connect",
    "unable to connect",
    "econnrefused",
    "econnreset",
    "etimedout",
    "enotfound",
    "socket hang up",
    "dns",
    "no such host",
    "temporary failure in name resolution",
)
AGY_PROVIDER_ERROR_PATTERNS = (
    "internal server error",
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "http 5",
    "status code 5",
    "status: 5",
    "5xx",
)
AGY_AUTH_PATTERNS = (
    "not signed in",
    "login",
    "authenticate",
    "oauth",
)
_NO_CODE_MAP = (
    "\n\nThe code map is not available for this agy call. Do not run `graphify`.\n"
)
LOGGER = logging.getLogger(__name__)


class AgyAgentRunner:
    """Run implementation and review agents through Antigravity's headless CLI."""

    # agy 1.5.0 reads MCP servers only from its saved mcp_config.json (managed
    # by ``agy mcp add``); ``--settings`` takes settings.json keys and has no
    # per-call MCP server. The harness never edits agy's configuration, so agy
    # cannot run the Playwright UI check. Provider choice skips agy for calls
    # of projects with ``ui_review`` enabled.
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
        self._version_logged = False
        self._version_lock = threading.Lock()

    def _pool_for_model(self, model: str) -> str | None:
        for pool_id, prefixes in self.config.agy.pools.items():
            if any(model.startswith(prefix) for prefix in prefixes):
                return pool_id
        return None

    def _implementation_profile(
        self,
        attempt: int,
        *,
        model_class: str | None = None,
        exhausted_pools: Iterable[str] = (),
    ) -> tuple[str, str | None]:
        if (model_class or "").strip().casefold() == "escalation":
            return self.config.agy.escalation_model, self.config.agy.escalation_effort

        sequence = self.config.agy.implementation_sequence
        start = min(max(0, int(attempt) - 1), len(sequence) - 1)
        exhausted = set(exhausted_pools)
        for entry in sequence[start:]:
            model = str(entry["model"])
            if self._pool_for_model(model) not in exhausted:
                effort = entry.get("effort")
                return model, str(effort) if effort is not None else None

        # S10-T5 normally prevents a call when every usable pool is exhausted.
        # Keep selection deterministic if this runner is called directly anyway.
        entry = sequence[-1]
        effort = entry.get("effort")
        return str(entry["model"]), str(effort) if effort is not None else None

    def _review_profile(self) -> tuple[str, str | None]:
        return self.config.agy.review_model, self.config.agy.review_effort

    def _command(
        self,
        model: str,
        effort: str | None,
        mode: str,
        settings: str,
    ) -> list[str]:
        # ``-p=`` (attached empty form, one argument) selects print mode; a
        # standalone ``-p`` would swallow the next flag as its prompt. The
        # actual, potentially long prompt goes to stdin as one stream-json
        # user message.
        command = [
            self.config.agy.command,
            "-p=",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--model",
            model,
        ]
        if effort and any(
            model.startswith(prefix) for prefix in self.config.agy.effort_models
        ):
            command += ["--effort", effort]
        elif effort:
            LOGGER.debug(
                "Omitting agy --effort for model %s because it does not match "
                "agy.effort_models",
                model,
            )
        command += ["--mode", mode, "--settings", settings]
        return command

    def _log_version_once(self, workspace: Path, env: dict[str, str] | None) -> None:
        with self._version_lock:
            if self._version_logged:
                return
            self._version_logged = True
            try:
                completed = subprocess.run(
                    prepare_external_argv([self.config.agy.command, "--version"]),
                    cwd=workspace,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=15,
                    env=env,
                )
                rendered = "".join((completed.stdout, completed.stderr)).strip()
                if not rendered:
                    rendered = f"agy --version exited {completed.returncode} without output"
            except (OSError, subprocess.TimeoutExpired) as exc:
                rendered = f"agy --version failed: {exc}"
            write_log(self.log_dir, "agy-version.log", rendered + "\n")

    @staticmethod
    def _parse_output(
        output: str,
    ) -> tuple[str, str | None, dict[str, int], list[str], str]:
        """Return response, status, usage, denied actions, and diagnostics."""
        final: dict[str, Any] | None = None
        diagnostics: list[str] = []
        for line in output.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                diagnostics.append(line)
                continue
            if not isinstance(event, dict):
                diagnostics.append(line)
                continue
            candidate = event.get("result") if event.get("event") == "result" else None
            if isinstance(candidate, dict):
                final = candidate

        if final is None:
            return "", None, {}, [], "\n".join(diagnostics)

        raw_usage = final.get("usage")
        usage: dict[str, int] = {}
        if isinstance(raw_usage, dict):
            for key in (
                "input_tokens",
                "output_tokens",
                "thinking_tokens",
                "cache_read_tokens",
            ):
                value = raw_usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    usage[key] = value

        denied: list[str] = []
        raw_denied = final.get("denied_actions")
        if isinstance(raw_denied, list):
            for item in raw_denied:
                if isinstance(item, dict):
                    action = str(item.get("action") or "").strip()
                    display = str(item.get("display_name") or "").strip()
                    label = display or action
                    if action and display and action != display:
                        label = f"{display} ({action})"
                    if label:
                        denied.append(label)
                elif isinstance(item, str) and item.strip():
                    denied.append(item.strip())

        reported_error = final.get("error")
        if isinstance(reported_error, str) and reported_error.strip():
            diagnostics.append(reported_error.strip())
        response = final.get("response")
        status = final.get("status")
        return (
            response if isinstance(response, str) else "",
            status if isinstance(status, str) else None,
            usage,
            denied,
            "\n".join(diagnostics),
        )

    @staticmethod
    def _matches(output: str, patterns: Iterable[str]) -> bool:
        lowered = output.replace("’", "'").replace("‘", "'").casefold()
        return any(
            pattern.replace("’", "'").casefold() in lowered for pattern in patterns
        )

    def _record_usage(
        self,
        task_id: str,
        phase: str,
        model: str,
        effort: str | None,
        attempt: int | None,
        result: AgentResult,
    ) -> None:
        if self.usage is None:
            return
        event: dict[str, object] = {
            "provider": "agy",
            "task_id": task_id,
            "phase": phase,
            "attempt": attempt,
            "model_class": "agy",
            "model": model,
            "effort": effort,
            "pool_id": self._pool_for_model(model),
            "duration_seconds": round(result.duration_seconds, 3),
            "ok": result.ok,
            "timed_out": result.timed_out,
            "stalled": result.stalled,
            "quota_exhausted": result.quota_exhausted,
            "capacity_exhausted": result.capacity_exhausted,
            "waiting_cause": result.waiting_cause,
            "auth_failure": result.auth_failure,
            "launch_failure": result.launch_failure,
            "code_map": False,
            "code_map_calls": 0,
            "input_tokens": result.input_tokens,
            "cached_input_tokens": result.cached_input_tokens,
            "output_tokens": result.output_tokens,
            "reasoning_output_tokens": result.reasoning_output_tokens,
            "output_bytes": len(result.output.encode("utf-8", errors="replace")),
        }
        if phase == "review":
            event["review_verdict"], event["finding_count"] = parse_review_outcome(
                result.ok, result.output
            )
        self.usage.record(event)

    @staticmethod
    def _stop_process(proc: subprocess.Popen[str]) -> None:
        stop_process_tree(proc)

    def _invoke(
        self,
        *,
        task_id: str,
        phase: str,
        prompt: str,
        workspace: Path,
        model: str,
        effort: str | None,
        mode: str,
        settings: str,
        timeout_minutes: int,
        log_name: str,
        attempt: int | None = None,
        env: dict[str, str] | None = None,
        on_process_start: Callable[[int], None] | None = None,
    ) -> AgentResult:
        self._log_version_once(workspace, env)
        command = self._command(model, effort, mode, settings)
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
                env=env,
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
            message = {"event": "user", "message": {"content": prompt}}
            try:
                proc.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            finally:
                proc.stdin.close()
        except OSError as exc:
            output = str(exc)
            write_log(self.log_dir, log_name, output)
            result = AgentResult(
                False,
                output,
                provider_could_not_start("agy", str(exc)),
                model=model,
                provider="agy",
                launch_failure=True,
            )
            self._record_usage(task_id, phase, model, effort, attempt, result)
            return result

        def read_output() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                with output_lock:
                    output_lines.append(line)
                    last_output_at[0] = time.monotonic()

        reader = threading.Thread(
            target=read_output, name=f"{task_id}-{phase}-agy-stdout", daemon=True
        )
        reader.start()

        hard_timeout = max(1, timeout_minutes) * 60.0
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
                error = f"Agent timed out after {timeout_minutes} minutes"
                self._stop_process(proc)
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
                    "Agent stalled: no CLI output or repository file activity for "
                    f"{self.config.execution.stall_timeout_minutes:g} minutes"
                )
                self._stop_process(proc)
                break
            try:
                proc.wait(timeout=poll)
            except subprocess.TimeoutExpired:
                pass

        return_code: int | None
        try:
            return_code = proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._stop_process(proc)
            return_code = proc.poll()
        reader.join(timeout=2)

        duration = time.monotonic() - started
        with output_lock:
            raw_output = "".join(output_lines)
        log_output = raw_output
        if error:
            log_output = (log_output + "\n" + error).strip() + "\n"
        write_log(self.log_dir, log_name, log_output)

        if return_code is not None and int(return_code) in WINDOWS_RESOURCE_EXIT_CODES:
            result = AgentResult(
                False,
                raw_output,
                resource_could_not_start("agy", int(return_code)),
                duration_seconds=duration,
                model=model,
                provider="agy",
                transient_error=True,
                resource_launch_failure=True,
            )
            self._record_usage(task_id, phase, model, effort, attempt, result)
            return result

        response, status, usage, denied, diagnostics = self._parse_output(raw_output)
        # Classify limit texts only on calls that failed, and only in stderr /
        # diagnostic lines and the result's own error field, never in the
        # streamed answer: a good answer may legitimately mention "rate limit".
        call_failed = (
            return_code != 0
            or timed_out
            or stalled
            or status != "SUCCESS"
        )
        classification = diagnostics
        if status is not None and status != "SUCCESS":
            classification = "\n".join((response, diagnostics))
        quota_exhausted = call_failed and self._matches(
            classification, AGY_QUOTA_PATTERNS
        )
        capacity_exhausted = (
            call_failed
            and not quota_exhausted
            and self._matches(classification, AGY_CAPACITY_PATTERNS)
        )
        auth_failure = (
            call_failed
            and not (timed_out or stalled)
            and not quota_exhausted
            and not capacity_exhausted
            and self._matches(classification, AGY_AUTH_PATTERNS)
        )
        waiting_cause: str | None
        if quota_exhausted:
            # A cap until the reset beats a timeout: report it as quota.
            waiting_cause = None
        elif timed_out or stalled:
            waiting_cause = "timeout"
        elif capacity_exhausted:
            waiting_cause = "provider_busy"
        elif call_failed and not quota_exhausted and not auth_failure:
            if self._matches(classification, AGY_NETWORK_PATTERNS):
                waiting_cause = "network"
            elif self._matches(classification, AGY_PROVIDER_ERROR_PATTERNS):
                waiting_cause = "provider_error"
            else:
                waiting_cause = None
        else:
            waiting_cause = None
        ok = (
            return_code == 0
            and not timed_out
            and not stalled
            and status == "SUCCESS"
            and not denied
            and not quota_exhausted
            and not capacity_exhausted
            and waiting_cause is None
            and not auth_failure
        )
        if denied:
            error = "Agy denied actions: " + ", ".join(denied)
        elif quota_exhausted:
            error = "Agy usage quota exhausted"
        elif capacity_exhausted:
            error = "Agy provider capacity temporarily exhausted"
        elif auth_failure:
            error = diagnostics or "Agy authentication unavailable"
        elif waiting_cause in {"network", "provider_error"}:
            error = f"Agy {waiting_cause.replace('_', ' ')} failure"
        elif status != "SUCCESS" and not timed_out and not stalled:
            error = diagnostics or f"Agy reported status {status or 'missing'}"
        elif return_code != 0 and not timed_out and not stalled:
            error = diagnostics or f"Agent exit code {return_code}"

        result = AgentResult(
            ok=ok,
            output=response,
            error=error,
            timed_out=timed_out,
            stalled=stalled,
            duration_seconds=duration,
            model=model,
            quota_exhausted=quota_exhausted,
            quota_scope=self._pool_for_model(model) if quota_exhausted else None,
            capacity_exhausted=capacity_exhausted,
            transient_error=waiting_cause
            in {"provider_busy", "network", "provider_error", "timeout"},
            waiting_cause=waiting_cause,
            auth_failure=auth_failure,
            provider="agy",
            input_tokens=usage.get("input_tokens"),
            cached_input_tokens=usage.get("cache_read_tokens"),
            output_tokens=usage.get("output_tokens"),
            reasoning_output_tokens=usage.get("thinking_tokens"),
        )
        self._record_usage(task_id, phase, model, effort, attempt, result)
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
        base_sha: str | None = None,
        exhausted_pools: Iterable[str] = (),
        on_process_start: Callable[[int], None] | None = None,
    ) -> AgentResult:
        del code_map_offered
        effective_class = model_class_override or task.model_class
        exhausted = set(exhausted_pools)
        while True:
            result = self._implement_once(
                task, workspace, timeout_minutes, attempt, previous_failure,
                env=env, model_class=effective_class, base_sha=base_sha,
                exhausted_pools=exhausted, on_process_start=on_process_start,
            )
            pool = result.quota_scope
            if (
                not result.quota_exhausted
                or pool is None
                or pool in exhausted
                or (effective_class or "").strip().casefold() == "escalation"
            ):
                return result
            exhausted.add(pool)
            if not self._has_entry_outside(attempt, exhausted):
                return result

    def _has_entry_outside(self, attempt: int, exhausted: set[str]) -> bool:
        sequence = self.config.agy.implementation_sequence
        start = min(max(0, int(attempt) - 1), len(sequence) - 1)
        return any(
            self._pool_for_model(str(entry["model"])) not in exhausted
            for entry in sequence[start:]
        )

    def _implement_once(
        self,
        task: TaskSpec,
        workspace: Path,
        timeout_minutes: int,
        attempt: int,
        previous_failure: str | None,
        *,
        env: dict[str, str] | None,
        model_class: str | None,
        base_sha: str | None,
        exhausted_pools: Iterable[str],
        on_process_start: Callable[[int], None] | None,
    ) -> AgentResult:
        model, effort = self._implementation_profile(
            attempt,
            model_class=model_class,
            exhausted_pools=exhausted_pools,
        )
        settings = build_settings(
            task.verification, self.config.agy.base_allow, base_sha=base_sha
        )
        prompt = implement_prompt(
            task,
            previous_failure,
            self._context_paths(task),
            False,
            self.config.code_map.command,
            ui_review=None,
        )
        return self._invoke(
            task_id=task.id,
            phase="implement",
            prompt=prompt + _NO_CODE_MAP,
            workspace=workspace,
            model=model,
            effort=effort,
            mode="accept-edits",
            settings=settings,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-attempt-{attempt}-agy.log",
            attempt=attempt,
            env=env,
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
        model, effort = self._review_profile()
        settings = build_settings(
            (), self.config.agy.base_allow, base_sha=base_ref
        )
        prompt = review_prompt(
            task,
            base_ref,
            verification_summary,
            self._context_paths(task),
            False,
            self.config.code_map.command,
            ui_review=None,
        )
        return self._invoke(
            task_id=task.id,
            phase="review",
            prompt=prompt + _NO_CODE_MAP,
            workspace=workspace,
            model=model,
            effort=effort,
            mode="plan",
            settings=settings,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-review-agy.log",
            env=env,
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
            model = self.config.agy.escalation_model
            effort: str | None = self.config.agy.escalation_effort
        else:
            model, effort = self._review_profile()
        return self._invoke(
            task_id=task.id,
            phase="plan_repair",
            prompt=(
                plan_change_prompt(task, failure_summary, self._context_paths(task))
                + _NO_CODE_MAP
            ),
            workspace=workspace,
            model=model,
            effort=effort,
            mode="plan",
            settings=build_settings((), self.config.agy.base_allow),
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-plan-repair-agy.log",
            env=env,
        )
