from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

from .agent_runner import AgentResult
from .config import HarnessConfig
from .context import ContextResolver
from .logging_utils import write_log
from .models import TaskSpec
from .prompts import implement_prompt, review_prompt
from .process_utils import cursor_prompt_via_stdin, prepare_external_argv, stop_process_tree
from .usage import UsageRecorder



class CursorAgentRunner:
    def __init__(
        self,
        config: HarnessConfig,
        log_dir: Path,
        context: ContextResolver | None = None,
        usage: UsageRecorder | None = None,
        *,
        repo_root: Path | None = None,
        worktree_root: Path | None = None,
    ):
        self.config = config
        self.log_dir = log_dir
        self.context = context
        self.usage = usage
        self.repo_root = repo_root.resolve() if repo_root is not None else None
        self.worktree_root = worktree_root.resolve() if worktree_root is not None else None


    def _is_verified_harness_worktree(self, workspace: Path) -> bool:
        """Return True only for a Git worktree owned by this harness runtime.

        `--trust` is intentionally never granted to the integration checkout or an
        arbitrary caller-supplied directory. The workspace must be a direct child
        of `.harness/worktrees`, be its own Git toplevel, and be registered by Git
        as a worktree of the integration repository.
        """
        if not self.config.cursor.trust_harness_worktrees:
            return False
        if self.repo_root is None or self.worktree_root is None:
            return False

        try:
            resolved = workspace.resolve()
            if resolved == self.repo_root or resolved.parent != self.worktree_root:
                return False
            if not resolved.is_relative_to(self.worktree_root):
                return False

            top = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=resolved,
                text=True, encoding="utf-8", errors="replace",
                capture_output=True,
                timeout=10,
            )
            if top.returncode != 0 or Path(top.stdout.strip()).resolve() != resolved:
                return False

            listed = subprocess.run(
                ["git", "worktree", "list", "--porcelain"],
                cwd=self.repo_root,
                text=True, encoding="utf-8", errors="replace",
                capture_output=True,
                timeout=10,
            )
            if listed.returncode != 0:
                return False
            registered = {
                Path(line[len("worktree "):]).resolve()
                for line in listed.stdout.splitlines()
                if line.startswith("worktree ")
            }
            return resolved in registered
        except (OSError, subprocess.TimeoutExpired, ValueError):
            return False

    @staticmethod
    def _workspace_signature(workspace: Path) -> tuple[int, int, int]:
        """Cheap-ish source activity signature for watchdog purposes.

        It inspects tracked + non-ignored untracked files instead of walking
        dependency caches. A changed mtime/size/count is enough to reset the
        inactivity timer; this is not used as a correctness hash.
        """
        try:
            proc = subprocess.run(
                ["git", "ls-files", "-co", "--exclude-standard", "-z"],
                cwd=workspace,
                capture_output=True,
                timeout=10,
            )
            if proc.returncode != 0:
                return (0, 0, 0)
            count = 0
            mtime_sum = 0
            total_size = 0
            for raw in proc.stdout.split(b"\0"):
                if not raw:
                    continue
                try:
                    rel = raw.decode("utf-8", errors="surrogateescape")
                    stat = (workspace / rel).stat()
                except OSError:
                    continue
                count += 1
                mtime_sum += stat.st_mtime_ns
                total_size += stat.st_size
            return (count, mtime_sum, total_size)
        except (OSError, subprocess.TimeoutExpired):
            return (0, 0, 0)

    @staticmethod
    def _stop_process(proc: subprocess.Popen[str]) -> None:
        # proc is the cmd/PowerShell shim on Windows; terminating it alone orphans the agent.
        stop_process_tree(proc)

    def _invoke(
        self,
        *,
        task_id: str,
        phase: str,
        prompt: str,
        workspace: Path,
        model_class: str,
        timeout_minutes: int,
        log_name: str,
        mode: str | None = None,
        attempt: int | None = None,
        env: dict[str, str] | None = None,
        model_class_override: str | None = None,
    ) -> AgentResult:
        pipe_prompt = cursor_prompt_via_stdin(self.config.cursor.command)
        command = [
            self.config.cursor.command,
            "-p",
        ]
        # On Windows the official Cursor CLI is a batch shim. Sending a large
        # multi-line prompt through that argv path is lossy; keep control flags
        # in argv and stream the prompt via stdin instead. Other platforms keep
        # the documented positional prompt form.
        if not pipe_prompt:
            command.append(prompt)
        command += [
            "--workspace",
            str(workspace),
            "--output-format",
            self.config.cursor.output_format,
        ]
        effective_model_class = model_class_override or model_class
        model = self.config.cursor.models.get(effective_model_class)
        if model:
            command += ["--model", model]
        if mode:
            command += [f"--mode={mode}"]
        verified_harness_worktree = self._is_verified_harness_worktree(workspace)
        if verified_harness_worktree:
            command += ["--trust"]
        # Cursor print/headless mode requires --force for unattended writes and
        # shell commands. Restrict it to implementation calls inside a verified
        # harness-owned worktree; read-only review/planning calls never receive it.
        if phase == "implement" and verified_harness_worktree:
            command += ["--force"]

        started = time.monotonic()
        output_lines: list[str] = []
        output_lock = threading.Lock()
        last_output_at = [started]
        timed_out = False
        stalled = False
        error: str | None = None
        return_code: int | None = None

        launch_command = prepare_external_argv(command)
        try:
            proc = subprocess.Popen(
                launch_command,
                cwd=workspace,
                text=True, encoding="utf-8", errors="replace",
                stdin=(subprocess.PIPE if pipe_prompt else None),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                bufsize=1,
                env=env,
                # Own process group, so stop_process_tree can never reach the harness itself.
                start_new_session=(os.name != "nt"),
                creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0),
            )
            if pipe_prompt:
                assert proc.stdin is not None
                try:
                    proc.stdin.write(prompt)
                    proc.stdin.flush()
                finally:
                    proc.stdin.close()
        except OSError as exc:
            output = str(exc)
            write_log(self.log_dir, log_name, output)
            result = AgentResult(False, output, f"Could not start Cursor CLI: {exc}", model=model)
            self._record_usage(task_id, phase, effective_model_class, model, attempt, result)
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
        signature = self._workspace_signature(workspace)
        last_repo_activity = started

        while proc.poll() is None:
            now = time.monotonic()
            if now - started >= hard_timeout:
                timed_out = True
                error = f"Agent timed out after {timeout_minutes} minutes"
                self._stop_process(proc)
                break

            new_signature = self._workspace_signature(workspace)
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
                # Wake immediately when a short-lived agent exits instead of
                # sleeping the full watchdog polling interval.
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

        quota_exhausted = self._looks_like_quota_exhaustion(output)
        capacity_exhausted = (
            not quota_exhausted and self._looks_like_capacity_exhaustion(output)
        )
        ok = (
            not timed_out
            and not stalled
            and return_code == 0
            and not quota_exhausted
            and not capacity_exhausted
        )
        if quota_exhausted:
            error = "Cursor usage quota exhausted"
        elif capacity_exhausted:
            error = "Cursor provider capacity temporarily exhausted"
        elif not ok and error is None:
            error = f"Agent exit code {return_code}"
        result = AgentResult(
            ok=ok,
            output=output,
            error=error,
            timed_out=timed_out,
            stalled=stalled,
            duration_seconds=duration,
            model=model,
            quota_exhausted=quota_exhausted,
            capacity_exhausted=capacity_exhausted,
        )
        self._record_usage(task_id, phase, effective_model_class, model, attempt, result)
        return result

    def _looks_like_quota_exhaustion(self, output: str) -> bool:
        lowered = output.replace("’", "'").replace("‘", "'").casefold()
        return any(pattern.casefold() in lowered for pattern in self.config.quota.exhaustion_patterns if pattern)

    def _looks_like_capacity_exhaustion(self, output: str) -> bool:
        lowered = output.casefold()
        return any(
            pattern.casefold() in lowered
            for pattern in self.config.capacity.exhaustion_patterns
            if pattern
        )

    def _record_usage(
        self,
        task_id: str,
        phase: str,
        model_class: str,
        model: str | None,
        attempt: int | None,
        result: AgentResult,
    ) -> None:
        if self.usage is None:
            return
        self.usage.record(
            {
                "provider": "cursor",
                "task_id": task_id,
                "phase": phase,
                "attempt": attempt,
                "model_class": model_class,
                "model": model,
                "duration_seconds": round(result.duration_seconds, 3),
                "ok": result.ok,
                "timed_out": result.timed_out,
                "stalled": result.stalled,
                "quota_exhausted": result.quota_exhausted,
                "capacity_exhausted": result.capacity_exhausted,
                "output_bytes": len(result.output.encode("utf-8", errors="replace")),
            }
        )

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
    ) -> AgentResult:
        return self._invoke(
            task_id=task.id,
            phase="implement",
            prompt=implement_prompt(task, previous_failure, self._context_paths(task)),
            workspace=workspace,
            model_class=task.model_class,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-attempt-{attempt}.log",
            attempt=attempt,
            env=env,
            model_class_override=model_class_override,
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
    ) -> AgentResult:
        return self._invoke(
            task_id=task.id,
            phase="review",
            prompt=review_prompt(task, base_ref, verification_summary, self._context_paths(task)),
            workspace=workspace,
            model_class=task.review_model_class or "reviewer",
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-review.log",
            mode="ask",
            env=env,
        )
    def plan_change(
        self,
        task: TaskSpec,
        workspace: Path,
        failure_summary: str,
        timeout_minutes: int,
        *,
        model_class: str,
        env: dict[str, str] | None = None,
    ) -> AgentResult:
        from .prompts import plan_change_prompt

        return self._invoke(
            task_id=task.id,
            phase="plan_repair",
            prompt=plan_change_prompt(task, failure_summary, self._context_paths(task)),
            workspace=workspace,
            model_class=model_class,
            timeout_minutes=timeout_minutes,
            log_name=f"{task.id}-plan-repair.log",
            mode="ask",
            env=env,
        )

