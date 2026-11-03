from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path

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
from .usage import UsageRecorder, count_code_map_calls

_SHELL_TOOL_NAMES = frozenset({"bash", "powershell", "shell"})

# Keep Claude's CLI/API vocabulary provider-local. A future Claude config section
# may extend these defaults without making Claude depend on Cursor's patterns.
CLAUDE_QUOTA_PATTERNS = (
    "usage limit",
    "limit reached",
    "rate_limit",
    "rate limit",
    "http 429",
    "status code 429",
    '"status_code": 429',
    '"status_code":429',
)
CLAUDE_NETWORK_PATTERNS = (
    "network error",
    "connection error",
    "connection failed",
    "failed to connect",
    "unable to connect",
    "connection lost",
    "connection reset",
    "connection refused",
    "socket hang up",
    "transport error",
    "econnrefused",
    "econnreset",
    "etimedout",
    "enotfound",
    "eai_again",
    "getaddrinfo",
)
CLAUDE_BUSY_PATTERNS = (
    "overloaded",
    "http 529",
    "status code 529",
    '"status_code": 529',
    '"status_code":529',
)
CLAUDE_PROVIDER_ERROR_PATTERNS = (
    "api_error",
    "api error: 5",
    "5xx",
    "internal server error",
    "service unavailable",
    "http 5",
    "status code 5",
    '"status_code": 5',
    '"status_code":5',
)
CLAUDE_AUTH_PATTERNS = (
    "not logged in",
    "authentication",
    "/login",
)


class ClaudeAgentRunner:
    """Non-interactive Claude Code provider adapter using stdin + stream-json."""

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

    @staticmethod
    def _stop_process(proc: subprocess.Popen[str]) -> None:
        stop_process_tree(proc)

    def _implementation_profile(
        self, attempt: int, *, model_class: str | None = None
    ) -> tuple[str, str]:
        if (model_class or "").strip().casefold() == "escalation":
            return (
                self.config.claude.escalation_model,
                self.config.claude.escalation_effort,
            )
        sequence = self.config.claude.implementation_sequence
        index = max(0, int(attempt) - 1)
        index = min(index, len(sequence) - 1)
        entry = sequence[index]
        return str(entry["model"]), str(entry["effort"])

    def _review_profile(self) -> tuple[str, str]:
        return self.config.claude.review_model, self.config.claude.review_effort

    def _command(self, model: str, effort: str) -> list[str]:
        return [
            self.config.claude.command,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            model,
            "--effort",
            effort,
            "--permission-mode",
            self.config.claude.permission_mode,
        ]

    @staticmethod
    def _playwright_mcp_payload(playwright_mcp_package: str) -> dict[str, object]:
        return {
            "mcpServers": {
                "playwright": {
                    "command": "npx",
                    "args": ["-y", playwright_mcp_package],
                }
            }
        }

    @staticmethod
    def _empty_mcp_payload() -> dict[str, object]:
        return {"mcpServers": {}}

    def _mcp_payload_for_phase(self, phase: str) -> dict[str, object] | None:
        # Implementation (including retries) and review share Playwright when UI
        # review is on; otherwise they get an empty config so --strict-mcp-config
        # does not load the user's own MCP servers. Plan repair stays unchanged.
        if phase not in {"implement", "review"}:
            return None
        if self.config.ui_review.enabled:
            return self._playwright_mcp_payload(self.config.ui_review.playwright_mcp_package)
        return self._empty_mcp_payload()

    def _write_mcp_config(self, task_id: str, phase: str, payload: dict[str, object]) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        path = self.log_dir / f"{task_id}-{phase}-claude-mcp.json"
        path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return path

    @staticmethod
    def _remove_mcp_config(path: Path | None) -> None:
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _parse_usage(reported: dict[str, object]) -> dict[str, int]:
        usage = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
        }
        token_fields = {
            "input_tokens": ("input_tokens", "inputTokens"),
            "cached_input_tokens": ("cache_read_input_tokens", "cacheReadTokens", "cacheReadInputTokens"),
            "cache_write_tokens": (
                "cache_creation_input_tokens",
                "cacheWriteTokens",
                "cacheCreationInputTokens",
            ),
            "output_tokens": ("output_tokens", "outputTokens"),
        }
        for target, sources in token_fields.items():
            for source in sources:
                if source not in reported:
                    continue
                value = reported.get(source, 0)
                if isinstance(value, bool) or value is None:
                    break
                if isinstance(value, (int, float)):
                    usage[target] = int(value)
                elif isinstance(value, str):
                    try:
                        usage[target] = int(value)
                    except ValueError:
                        pass
                break
        return usage

    @staticmethod
    def _tool_use_blocks(event: dict[str, object]) -> list[dict[str, object]]:
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            return []
        blocks: list[dict[str, object]] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "tool_use":
                blocks.append(part)
        return blocks

    @classmethod
    def _parse_output(
        cls, raw_output: str, code_map_command: str = ""
    ) -> tuple[str, str, dict[str, int], int, int, bool]:
        """Extract answer, diagnostics, usage, tool calls, code-map calls, error flag."""
        assistant_messages: list[str] = []
        result_text: str | None = None
        diagnostics: list[str] = []
        commands: list[str] = []
        tool_calls = 0
        usage = {
            "input_tokens": 0,
            "cached_input_tokens": 0,
            "cache_write_tokens": 0,
            "output_tokens": 0,
        }
        saw_json = False
        reported_error = False

        for raw in raw_output.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                diagnostics.append(line)
                continue
            if not isinstance(event, dict):
                continue
            saw_json = True
            event_type = event.get("type")
            if event_type == "assistant":
                message = event.get("message")
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, list):
                    text = "".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict)
                        and part.get("type") == "text"
                        and isinstance(part.get("text"), str)
                    )
                    if text:
                        assistant_messages.append(text)
                for block in cls._tool_use_blocks(event):
                    tool_calls += 1
                    name = str(block.get("name") or "").casefold()
                    if name not in _SHELL_TOOL_NAMES:
                        continue
                    tool_input = block.get("input")
                    command = tool_input.get("command") if isinstance(tool_input, dict) else None
                    if isinstance(command, str):
                        commands.append(command)
            elif event_type == "result":
                value = event.get("result")
                if isinstance(value, str):
                    result_text = value
                reported_error = event.get("is_error") is True
                reported = event.get("usage")
                if isinstance(reported, dict):
                    usage = cls._parse_usage(reported)
                if reported_error:
                    diagnostics.append(json.dumps(event, ensure_ascii=False))

        if result_text is not None:
            if assistant_messages and "".join(assistant_messages) == result_text:
                answer = "\n".join(assistant_messages)
            else:
                answer = result_text
        elif assistant_messages:
            answer = assistant_messages[-1]
        else:
            answer = raw_output
        if not saw_json and raw_output.strip() and not diagnostics:
            diagnostics.append(raw_output.strip())
        return (
            answer,
            "\n".join(diagnostics),
            usage,
            tool_calls,
            count_code_map_calls(commands, code_map_command),
            reported_error,
        )

    @staticmethod
    def _looks_like_quota_exhaustion(output: str) -> bool:
        lowered = output.replace("’", "'").replace("‘", "'").casefold()
        return any(pattern.casefold() in lowered for pattern in CLAUDE_QUOTA_PATTERNS)

    @staticmethod
    def _matches_patterns(output: str, patterns: tuple[str, ...]) -> bool:
        lowered = output.casefold()
        return any(pattern.casefold() in lowered for pattern in patterns)

    def _looks_like_auth_failure(self, output: str) -> bool:
        return self._matches_patterns(output, CLAUDE_AUTH_PATTERNS)

    def _waiting_cause(self, output: str) -> str | None:
        if self._matches_patterns(output, CLAUDE_NETWORK_PATTERNS):
            return "network"
        if self._matches_patterns(output, CLAUDE_BUSY_PATTERNS):
            return "provider_busy"
        if self._matches_patterns(output, CLAUDE_PROVIDER_ERROR_PATTERNS):
            return "provider_error"
        return None

    def _record_usage(
        self,
        task_id: str,
        phase: str,
        model: str,
        effort: str,
        attempt: int | None,
        result: AgentResult,
        code_map: bool = False,
        code_map_calls: int = 0,
        tool_calls: int = 0,
        cache_write_tokens: int | None = None,
    ) -> None:
        if self.usage is None:
            return
        self.usage.record(
            {
                "provider": "claude",
                "task_id": task_id,
                "phase": phase,
                "attempt": attempt,
                "model_class": "claude",
                "model": model,
                "effort": effort,
                "duration_seconds": round(result.duration_seconds, 3),
                "ok": result.ok,
                "timed_out": result.timed_out,
                "stalled": result.stalled,
                "quota_exhausted": result.quota_exhausted,
                "capacity_exhausted": result.capacity_exhausted,
                "waiting_cause": result.waiting_cause,
                "auth_failure": result.auth_failure,
                "launch_failure": result.launch_failure,
                "code_map": code_map,
                "code_map_calls": code_map_calls,
                "tool_calls": tool_calls,
                "input_tokens": result.input_tokens,
                "cached_input_tokens": result.cached_input_tokens,
                "cache_write_tokens": cache_write_tokens,
                "output_tokens": result.output_tokens,
                "output_bytes": len(result.output.encode("utf-8", errors="replace")),
            }
        )

    def _invoke(
        self,
        *,
        task_id: str,
        phase: str,
        prompt: str,
        workspace: Path,
        model: str,
        effort: str,
        timeout_minutes: int,
        log_name: str,
        attempt: int | None = None,
        env: dict[str, str] | None = None,
        code_map: bool = False,
        on_process_start: Callable[[int], None] | None = None,
    ) -> AgentResult:
        command = self._command(model, effort)
        mcp_path: Path | None = None
        mcp_payload = self._mcp_payload_for_phase(phase)
        if mcp_payload is not None:
            mcp_path = self._write_mcp_config(task_id, phase, mcp_payload)
            command += ["--mcp-config", str(mcp_path), "--strict-mcp-config"]
        started = time.monotonic()
        output_lines: list[str] = []
        output_lock = threading.Lock()
        last_output_at = [started]
        timed_out = False
        stalled = False
        error: str | None = None
        return_code: int | None = None

        try:
            launch_command = prepare_external_argv(command)
            try:
                proc = subprocess.Popen(
                    launch_command,
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
                        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
                    ),
                )
                if on_process_start is not None:
                    on_process_start(proc.pid)
                assert proc.stdin is not None
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.flush()
                finally:
                    proc.stdin.close()
            except OSError as exc:
                output = str(exc)
                write_log(self.log_dir, log_name, output)
                result = AgentResult(
                    False,
                    output,
                    provider_could_not_start("claude", str(exc)),
                    model=model,
                    provider="claude",
                    launch_failure=True,
                )
                self._record_usage(task_id, phase, model, effort, attempt, result, code_map)
                return result

            def read_output() -> None:
                assert proc.stdout is not None
                for line in proc.stdout:
                    with output_lock:
                        output_lines.append(line)
                        last_output_at[0] = time.monotonic()

            reader = threading.Thread(target=read_output, name=f"{task_id}-{phase}-stdout", daemon=True)
            reader.start()

            hard_timeout = max(1, timeout_minutes) * 60.0
            stall_timeout = max(0.0, float(self.config.execution.stall_timeout_minutes)) * 60.0
            poll = max(0.1, float(self.config.execution.watchdog_poll_seconds))
            # Same activity rule as Cursor: CLI output or worktree file changes.
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
                last_activity = max(output_activity, last_repo_activity)
                if stall_timeout > 0 and now - last_activity >= stall_timeout:
                    stalled = True
                    error = (
                        f"Agent stalled: no CLI output or repository file activity for "
                        f"{self.config.execution.stall_timeout_minutes:g} minutes"
                    )
                    self._stop_process(proc)
                    break
                try:
                    proc.wait(timeout=poll)
                except subprocess.TimeoutExpired:
                    pass

            try:
                return_code = proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._stop_process(proc)
                return_code = proc.poll()
            reader.join(timeout=2)

            duration = time.monotonic() - started
            with output_lock:
                output = "".join(output_lines)
            if error:
                output = (output + "\n" + error).strip() + "\n"
            write_log(self.log_dir, log_name, output)

            if return_code is not None and int(return_code) in WINDOWS_RESOURCE_EXIT_CODES:
                resource_error = resource_could_not_start("claude", int(return_code))
                result = AgentResult(
                    False,
                    output,
                    resource_error,
                    duration_seconds=duration,
                    model=model,
                    provider="claude",
                    transient_error=True,
                    resource_launch_failure=True,
                )
                self._record_usage(task_id, phase, model, effort, attempt, result, code_map)
                return result

            final_output, diagnostics, usage, tool_calls, code_map_calls, reported_error = (
                self._parse_output(output, self.config.code_map.command)
            )
            classification_text = "\n".join(part for part in (final_output, diagnostics) if part)
            classify_failure = return_code != 0 or timed_out or stalled or reported_error
            waiting_cause: str | None
            if timed_out or stalled:
                quota_exhausted = False
                auth_failure = False
                waiting_cause = "timeout"
            else:
                quota_exhausted = classify_failure and self._looks_like_quota_exhaustion(
                    classification_text
                )
                auth_failure = (
                    classify_failure
                    and not quota_exhausted
                    and self._looks_like_auth_failure(classification_text)
                )
                waiting_cause = (
                    self._waiting_cause(classification_text)
                    if classify_failure and not quota_exhausted and not auth_failure
                    else None
                )
            capacity_exhausted = waiting_cause == "provider_busy"
            ok = (
                not timed_out
                and not stalled
                and return_code == 0
                and not reported_error
                and not quota_exhausted
                and waiting_cause is None
            )
            if quota_exhausted:
                error = "Claude usage quota exhausted"
            elif auth_failure:
                error = diagnostics or final_output or "Claude authentication unavailable"
            elif capacity_exhausted:
                error = "Claude provider capacity temporarily exhausted"
            elif waiting_cause is not None:
                error = f"Claude {waiting_cause.replace('_', ' ')} failure"
            elif reported_error:
                error = "Claude reported an error"
            elif not ok and error is None:
                error = f"Agent exit code {return_code}"
            result = AgentResult(
                ok=ok,
                output=final_output,
                error=error,
                timed_out=timed_out,
                stalled=stalled,
                duration_seconds=duration,
                model=model,
                quota_exhausted=quota_exhausted,
                capacity_exhausted=capacity_exhausted,
                transient_error=waiting_cause in {"network", "provider_error", "timeout"},
                waiting_cause=waiting_cause,
                auth_failure=auth_failure,
                provider="claude",
                input_tokens=usage["input_tokens"] or None,
                cached_input_tokens=usage["cached_input_tokens"] or None,
                output_tokens=usage["output_tokens"] or None,
            )
            self._record_usage(
                task_id,
                phase,
                model,
                effort,
                attempt,
                result,
                code_map,
                code_map_calls,
                tool_calls,
                usage["cache_write_tokens"] or None,
            )
            return result
        finally:
            self._remove_mcp_config(mcp_path)

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
    ) -> AgentResult:
        ui = self.config.ui_review if self.config.ui_review.enabled else None
        model, effort = self._implementation_profile(
            attempt, model_class=model_class_override or task.model_class
        )
        return self._invoke(
            task_id=task.id,
            phase="implement",
            prompt=implement_prompt(
                task,
                previous_failure,
                self._context_paths(task),
                code_map_offered,
                self.config.code_map.command,
                ui_review=ui,
            ),
            workspace=workspace,
            model=model,
            effort=effort,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-attempt-{attempt}-claude.log",
            attempt=attempt,
            env=env,
            code_map=code_map_offered,
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
        ui = self.config.ui_review if self.config.ui_review.enabled else None
        model, effort = self._review_profile()
        return self._invoke(
            task_id=task.id,
            phase="review",
            prompt=review_prompt(
                task,
                base_ref,
                verification_summary,
                self._context_paths(task),
                code_map_offered,
                self.config.code_map.command,
                ui_review=ui,
            ),
            workspace=workspace,
            model=model,
            effort=effort,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-review-claude.log",
            env=env,
            code_map=code_map_offered,
            on_process_start=on_process_start,
        )

    def plan_change(
        self,
        task: TaskSpec,
        workspace: Path,
        failure_summary: str,
        timeout_minutes: int,
        *,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        model, effort = self._review_profile()
        return self._invoke(
            task_id=task.id,
            phase="plan_repair",
            prompt=plan_change_prompt(task, failure_summary, self._context_paths(task)),
            workspace=workspace,
            model=model,
            effort=effort,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-plan-repair-claude.log",
            env=env,
        )
