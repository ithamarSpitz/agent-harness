from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from pathlib import Path

from .config import HarnessConfig
from .context import ContextResolver
from .logging_utils import write_log
from .models import TaskSpec
from .process_utils import prepare_external_argv, resolve_codex_argv, stop_process_tree
from .prompts import implement_prompt, plan_change_prompt, review_prompt
from .agent_runner import AgentResult
from .usage import UsageRecorder


class CodexAgentRunner:
    """Non-interactive Codex provider adapter using stdin + JSONL."""

    def __init__(self, config: HarnessConfig, log_dir: Path, context: ContextResolver | None = None,
                 usage: UsageRecorder | None = None):
        self.config = config
        self.log_dir = log_dir
        self.context = context
        self.usage = usage
        self._availability: tuple[bool, str] | None = None

    @staticmethod
    def _stop_process(proc: subprocess.Popen[str]) -> None:
        stop_process_tree(proc)

    def availability(self, *, refresh: bool = False) -> tuple[bool, str]:
        if self._availability is not None and not refresh:
            return self._availability
        try:
            version = subprocess.run(
                prepare_external_argv(resolve_codex_argv(self.config.codex.command, "--version")),
                text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=15,
            )
            if version.returncode != 0:
                self._availability = (False, ((version.stdout or "") + (version.stderr or "")).strip() or "codex --version failed")
                return self._availability
            login = subprocess.run(
                prepare_external_argv(resolve_codex_argv(self.config.codex.command, "login", "status")),
                text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=20,
            )
            text = ((login.stdout or "") + ("\n" + login.stderr if login.stderr else "")).strip()
            required = self.config.codex.auth_required_substring
            ok = login.returncode == 0 and (not required or required.casefold() in text.casefold())
            self._availability = (ok, text or ("authenticated" if ok else "Codex is not logged in using ChatGPT"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            self._availability = (False, str(exc))
        return self._availability

    @staticmethod
    def _matches(text: str, patterns: list[str]) -> bool:
        # Codex prints typographic apostrophes ("You’ve hit your usage limit"); patterns use ASCII.
        lowered = text.replace("’", "'").replace("‘", "'").casefold()
        return any(p and p.casefold() in lowered for p in patterns)

    def _classify(self, diagnostics: str, *, timed_out: bool, start_error: bool = False) -> str | None:
        if self._matches(diagnostics, self.config.codex.short_quota_patterns):
            return "quota_5h"
        if self._matches(diagnostics, self.config.codex.weekly_quota_patterns):
            return "quota_weekly"
        if self._matches(diagnostics, self.config.codex.quota_patterns):
            return "quota"
        if self._matches(diagnostics, self.config.codex.auth_patterns):
            return "auth"
        if self._matches(diagnostics, self.config.codex.model_unavailable_patterns):
            return "model_unavailable"
        if self._matches(diagnostics, self.config.codex.capacity_patterns):
            return "capacity"
        if timed_out or start_error or self._matches(diagnostics, self.config.codex.transient_patterns):
            return "transient"
        return None

    def _effective_sandbox(self, *, read_only: bool, windows: bool | None = None) -> str:
        if read_only:
            return "read-only"
        is_windows = os.name == "nt" if windows is None else windows
        if is_windows and self.config.codex.windows_implementation_sandbox:
            return self.config.codex.windows_implementation_sandbox
        return self.config.codex.sandbox

    @staticmethod
    def _parse_events(raw_stdout: str) -> tuple[str, str, dict[str, int]]:
        messages: list[str] = []
        diagnostics: list[str] = []
        usage = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
        saw_json = False
        for raw in raw_stdout.splitlines():
            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            saw_json = True
            typ = str(event.get("type") or "")
            item = event.get("item") or {}
            if typ == "item.completed" and item.get("type") == "agent_message":
                text = item.get("text")
                if isinstance(text, str):
                    messages.append(text)
            if typ in {"error", "turn.failed"}:
                diagnostics.append(json.dumps(event, ensure_ascii=False))
            if typ == "turn.completed" and isinstance(event.get("usage"), dict):
                u = event["usage"]
                for key in usage:
                    try:
                        usage[key] += int(u.get(key, 0) or 0)
                    except (TypeError, ValueError):
                        pass
        if not saw_json and raw_stdout.strip():
            diagnostics.append(raw_stdout.strip())
        return (messages[-1] if messages else "", "\n".join(diagnostics), usage)

    def _record_usage(self, task_id: str, phase: str, model: str, attempt: int | None, result: AgentResult) -> None:
        if self.usage is None:
            return
        self.usage.record({
            "provider": "codex", "task_id": task_id, "phase": phase, "attempt": attempt,
            "model_class": "codex", "model": model, "duration_seconds": round(result.duration_seconds, 3),
            "ok": result.ok, "timed_out": result.timed_out, "stalled": result.stalled,
            "quota_exhausted": result.quota_exhausted, "quota_scope": result.quota_scope,
            "capacity_exhausted": result.capacity_exhausted,
            "transient_error": result.transient_error, "auth_failure": result.auth_failure,
            "model_unavailable": result.model_unavailable, "input_tokens": result.input_tokens,
            "cached_input_tokens": result.cached_input_tokens, "output_tokens": result.output_tokens,
            "reasoning_output_tokens": result.reasoning_output_tokens,
            "output_bytes": len(result.output.encode("utf-8", errors="replace")),
        })

    def _invoke(self, *, task_id: str, phase: str, prompt: str, workspace: Path, model: str,
                timeout_minutes: int, log_name: str, attempt: int | None = None,
                env: dict[str, str] | None = None, read_only: bool = False,
                reasoning_effort: str | None = None) -> AgentResult:
        sandbox_mode = self._effective_sandbox(read_only=read_only)
        effective_reasoning_effort = reasoning_effort or self.config.codex.reasoning_effort
        command = resolve_codex_argv(
            self.config.codex.command, "exec", "--json", "--ephemeral",
            "--model", model,
            "--sandbox", sandbox_mode,
            "--cd", str(workspace),
            "--config", f'model_reasoning_effort="{effective_reasoning_effort}"',
            "--config", f'approval_policy="{self.config.codex.approval_policy}"',
        )
        if not read_only and sandbox_mode == "workspace-write":
            command += ["--config", f"sandbox_workspace_write.network_access={'true' if self.config.codex.network_access else 'false'}"]

        started = time.monotonic()
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        lock = threading.Lock()
        last_output = [started]
        timed_out = False
        stalled = False
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(
                prepare_external_argv(command), cwd=workspace,
                text=True, encoding="utf-8", errors="replace",
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=1, env=env,
                start_new_session=(os.name != "nt"),
                creationflags=creationflags,
            )
            assert proc.stdin is not None
            proc.stdin.write(prompt)
            proc.stdin.close()
        except OSError as exc:
            result = AgentResult(False, "", f"Could not start Codex CLI: {exc}",
                                 duration_seconds=time.monotonic() - started, model=model,
                                 transient_error=True, provider="codex")
            write_log(self.log_dir, log_name, str(exc))
            self._record_usage(task_id, phase, model, attempt, result)
            return result

        def read(stream, target):
            if stream is None:
                return
            for line in stream:
                with lock:
                    target.append(line)
                    last_output[0] = time.monotonic()

        tout = threading.Thread(target=read, args=(proc.stdout, stdout_lines), daemon=True)
        terr = threading.Thread(target=read, args=(proc.stderr, stderr_lines), daemon=True)
        tout.start()
        terr.start()
        hard_timeout = max(0.01, float(timeout_minutes)) * 60.0
        stall_timeout = max(0.0, float(self.config.execution.stall_timeout_minutes)) * 60.0
        poll = max(0.1, float(self.config.execution.watchdog_poll_seconds))
        while proc.poll() is None:
            now = time.monotonic()
            if now - started >= hard_timeout:
                timed_out = True
                self._stop_process(proc)
                break
            with lock:
                last = last_output[0]
            if stall_timeout > 0 and now - last >= stall_timeout:
                stalled = True
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
            polled_return_code = proc.poll()
            return_code = polled_return_code if polled_return_code is not None else -1
        tout.join(timeout=2)
        terr.join(timeout=2)
        duration = time.monotonic() - started
        with lock:
            raw_stdout = "".join(stdout_lines)
            raw_stderr = "".join(stderr_lines)
        final_message, event_diagnostics, usage = self._parse_events(raw_stdout)
        diagnostics = "\n".join(x for x in [event_diagnostics, raw_stderr] if x.strip())
        if timed_out:
            diagnostics = (diagnostics + f"\nCodex timed out after {timeout_minutes} minutes").strip()
        if stalled:
            diagnostics = (diagnostics + f"\nCodex stalled after {self.config.execution.stall_timeout_minutes:g} minutes of silence").strip()
        log_body = raw_stdout + (("\n--- STDERR ---\n" + raw_stderr) if raw_stderr else "")
        write_log(self.log_dir, log_name, log_body)

        kind = self._classify(diagnostics, timed_out=(timed_out or stalled))
        if return_code != 0 and kind is None:
            kind = "transient"
        if return_code == 0 and not final_message and kind is None:
            kind = "transient"
            diagnostics = (diagnostics + "\nCodex JSON stream contained no final agent_message").strip()
        ok = return_code == 0 and not timed_out and not stalled and kind is None
        errors = {
            "quota": "Codex usage quota exhausted",
            "quota_5h": "Codex 5-hour usage quota exhausted",
            "quota_weekly": "Codex weekly usage quota exhausted",
            "capacity": "Codex provider capacity temporarily exhausted",
            "transient": diagnostics or "Codex transient execution failure",
            "auth": diagnostics or "Codex authentication unavailable",
            "model_unavailable": diagnostics or f"Codex model unavailable: {model}",
        }
        result = AgentResult(
            ok=ok, output=final_message, error=(errors.get(kind) if kind is not None else None), timed_out=timed_out, stalled=stalled,
            duration_seconds=duration, model=model, quota_exhausted=bool(kind and kind.startswith("quota")),
            quota_scope=("5h" if kind == "quota_5h" else ("weekly" if kind == "quota_weekly" else ("unspecified" if kind == "quota" else None))),
            capacity_exhausted=(kind == "capacity"), transient_error=(kind == "transient"),
            auth_failure=(kind == "auth"), model_unavailable=(kind == "model_unavailable"), provider="codex",
            input_tokens=usage["input_tokens"] or None, cached_input_tokens=usage["cached_input_tokens"] or None,
            output_tokens=usage["output_tokens"] or None, reasoning_output_tokens=usage["reasoning_output_tokens"] or None,
        )
        self._record_usage(task_id, phase, model, attempt, result)
        return result

    def _context_paths(self, task: TaskSpec) -> list[str]:
        return self.context.display_paths(task) if self.context is not None else []

    def implement(self, task: TaskSpec, workspace: Path, timeout_minutes: int, attempt: int,
                  previous_failure: str | None, *, model: str, reasoning_effort: str | None = None,
                  env: dict[str, str] | None = None) -> AgentResult:
        return self._invoke(
            task_id=task.id, phase="implement",
            prompt=implement_prompt(task, previous_failure, self._context_paths(task)), workspace=workspace,
            model=model, timeout_minutes=timeout_minutes, log_name=f"{task.id}-attempt-{attempt}-codex.log",
            attempt=attempt, env=env, read_only=False, reasoning_effort=reasoning_effort,
        )

    def review(self, task: TaskSpec, workspace: Path, base_ref: str, verification_summary: str,
               timeout_minutes: int, *, model: str, env: dict[str, str] | None = None) -> AgentResult:
        return self._invoke(
            task_id=task.id, phase="review",
            prompt=review_prompt(task, base_ref, verification_summary, self._context_paths(task)), workspace=workspace,
            model=model, timeout_minutes=timeout_minutes, log_name=f"{task.id}-review-codex.log",
            env=env, read_only=True,
        )

    def plan_change(self, task: TaskSpec, workspace: Path, failure_summary: str, timeout_minutes: int,
                    *, model: str, env: dict[str, str] | None = None) -> AgentResult:
        return self._invoke(
            task_id=task.id, phase="plan_repair",
            prompt=plan_change_prompt(task, failure_summary, self._context_paths(task)), workspace=workspace,
            model=model, timeout_minutes=timeout_minutes, log_name=f"{task.id}-plan-repair-codex.log",
            env=env, read_only=True,
        )
