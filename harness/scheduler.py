from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from .admission import Admission, can_start
from .agent_ledger import LedgerStore
from .code_map import CodeMapBuilder
from .config import HarnessConfig
from .environment import EnvironmentManager
from .events import append_event
from .failure import FailureRecord, FailureStore
from .git_worktree import GitError, WorktreeManager
from .logging_utils import utc_now, write_log
from .memory import free_memory_gb as default_free_memory_gb
from .models import (
    RESOURCE_LAUNCH_FAILURE_LIMIT,
    SprintSpec,
    TaskSpec,
    TaskStatus,
)
from .agent_runner import AgentResult
from .process_utils import stop_process_tree
from .proc_tree import PeakSampler
from .provider_runner import ProviderAgentRunner
from .release import ReleaseRunner, sprint_is_complete
from .roadmap import Roadmap
from .state import StateStore
from .task_queue import TaskQueueManager
from .verifier import Verifier, _CommandOutcome, _SUBPROCESS_RUN

_WAITING_EVENT_STATUSES = frozenset(
    {
        TaskStatus.WAITING_QUOTA,
        TaskStatus.WAITING_NETWORK,
        TaskStatus.WAITING_PROVIDER_BUSY,
        TaskStatus.WAITING_PROVIDER_ERROR,
        TaskStatus.WAITING_TIMEOUT,
    }
)


LOGGER = logging.getLogger(__name__)
FreeMemoryReader = Callable[[], float | None]
SamplerFactory = Callable[[int, float], PeakSampler]
ProcessStartHook = Callable[[int], None]


def _subprocess_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


@dataclass(slots=True)
class TaskOutcome:
    task_id: str
    ok: bool
    error: str | None = None
    quota_paused: bool = False
    capacity_paused: bool = False
    resource_paused: bool = False
    waiting_status: TaskStatus | None = None
    provider: str | None = None


class Scheduler:
    def __init__(
        self,
        repo_root: Path,
        config: HarnessConfig,
        state: StateStore,
        worktrees: WorktreeManager,
        runner: ProviderAgentRunner,
        verifier: Verifier,
        environment: EnvironmentManager | None = None,
        failures: FailureStore | None = None,
        roadmap: Roadmap | None = None,
        roadmap_path: Path | None = None,
        clock: Callable[[], float] = time.monotonic,
        free_memory_gb: FreeMemoryReader | None = None,
        ledger: LedgerStore | None = None,
        sampler_factory: SamplerFactory | None = None,
    ):
        self.repo_root = repo_root
        self.config = config
        self.state = state
        self.worktrees = worktrees
        self.runner = runner
        self.verifier = verifier
        self.environment = environment or EnvironmentManager(repo_root, config, verifier.log_dir)
        self.failures = failures or FailureStore(verifier.log_dir.parent / "failures")
        self.roadmap = roadmap
        self.roadmap_path = roadmap_path
        self._clock = clock
        self._free_memory_gb = free_memory_gb or default_free_memory_gb
        self._ledger = ledger or LedgerStore()
        self._sampler_factory = sampler_factory or PeakSampler
        # Unit-test runners execute in-process and have no agent process tree to
        # sample. An explicit factory still enables deterministic scheduler tests.
        self._sampling_enabled = (
            sampler_factory is not None or isinstance(runner, ProviderAgentRunner)
        )
        self._memory_estimate_lock = threading.Lock()
        # Concurrent CHECKING phases share one Verifier; route each thread's
        # verification Popen PIDs through a thread-local hook (same isolation
        # idea as the former _PopenObserver, without replacing subprocess.Popen).
        self._verify_hook_local = threading.local()
        self._verify_hook_lock = threading.Lock()
        self._verify_hook_users = 0
        self._verify_hook_original: Callable[..., _CommandOutcome] | None = None
        self._verify_hook_installed: Callable[..., _CommandOutcome] | None = None
        self._verify_hook_had_instance = False
        self._runtime_root = verifier.log_dir.parent
        self.task_queue = TaskQueueManager(repo_root, self._runtime_root, state)
        self._merge_in_progress: tuple[str, int] | None = None
        self._memory_fallback_logged = False
        self.code_maps = CodeMapBuilder(
            config.code_map, verifier.log_dir.parent, verifier.log_dir, repo_root
        )

    def current_memory_estimate(self) -> float:
        """GB reserved for one new agent, preferring the learned project value."""
        stored = self.state.get_meta("resources.memory_per_agent_gb")
        try:
            if isinstance(stored, (int, float, str)):
                return float(stored)
        except (TypeError, ValueError):
            LOGGER.warning("Ignoring invalid learned memory estimate: %r", stored)
        return float(self.config.execution.memory_per_agent_gb)

    @contextmanager
    def _process_sampler(
        self, observed_peaks: list[float]
    ) -> Iterator[ProcessStartHook | None]:
        """Yield an on_process_start hook that records peaks for one call."""
        if not self._sampling_enabled:
            yield None
            return
        samplers: list[PeakSampler] = []

        def start_sampler(pid: int) -> None:
            try:
                sampler = self._sampler_factory(
                    pid, float(self.config.execution.watchdog_poll_seconds)
                )
                sampler.start()
                samplers.append(sampler)
            except Exception as exc:
                LOGGER.warning(
                    "Memory peak sampler could not start for process %s: %s", pid, exc
                )

        try:
            yield start_sampler
        finally:
            for sampler in samplers:
                try:
                    peak = float(sampler.stop())
                    if peak > 0:
                        observed_peaks.append(peak)
                except Exception as exc:
                    LOGGER.warning("Memory peak sampler could not stop: %s", exc)

    def _run_timed_verification_command(
        self,
        command: str,
        workspace: Path,
        env: dict[str, str],
        timeout_seconds: float,
        on_process_start: ProcessStartHook,
        original: Callable[..., _CommandOutcome],
    ) -> _CommandOutcome:
        """Run one verification command via scheduler-owned Popen and report its PID.

        Mirrors ``Verifier._run_timed_command`` so verification peaks stay in the
        learned estimate without a process-wide ``subprocess.Popen`` wrapper.
        """
        if subprocess.run is not _SUBPROCESS_RUN:
            return original(command, workspace, env, timeout_seconds)

        timeout_minutes = float(timeout_seconds) / 60.0
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
            start_new_session=(os.name != "nt"),
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
            ),
        )
        try:
            on_process_start(proc.pid)
        except Exception as exc:
            LOGGER.warning(
                "Memory peak sampler could not observe verification process %s: %s",
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
        returncode = (
            -1 if timed_out else int(proc.returncode if proc.returncode is not None else -1)
        )
        return _CommandOutcome(returncode, output, timed_out, timeout_minutes)

    def _dispatch_timed_verification_command(
        self,
        command: str,
        workspace: Path,
        command_env: dict[str, str],
        timeout_seconds: float,
    ) -> _CommandOutcome:
        """Shared Verifier._run_timed_command stand-in; uses this thread's hook."""
        original = self._verify_hook_original
        assert original is not None
        hook = getattr(self._verify_hook_local, "on_process_start", None)
        if hook is None:
            return original(command, workspace, command_env, timeout_seconds)
        return self._run_timed_verification_command(
            command,
            workspace,
            command_env,
            timeout_seconds,
            hook,
            original,
        )

    def _verify_with_process_hook(
        self,
        task: TaskSpec,
        worktree: Path,
        base_ref: str,
        task_env: dict[str, str],
        on_process_start: ProcessStartHook | None,
    ):
        """Run task verification, reporting each command PID to the sampler hook.

        Concurrent tasks share one Verifier. Install a refcounted dispatcher once
        and bind ``on_process_start`` on a thread-local so overlapping CHECKING
        phases cannot steal or restore each other's hooked method.
        """
        if on_process_start is None:
            return self.verifier.verify_task(task, worktree, base_ref, env=task_env)

        verifier = self.verifier
        with self._verify_hook_lock:
            if self._verify_hook_users == 0:
                # Capture the exact bound-method object we install so restore
                # can use identity (re-fetching the attribute builds a new one).
                installed = self._dispatch_timed_verification_command
                self._verify_hook_had_instance = "_run_timed_command" in verifier.__dict__
                self._verify_hook_original = verifier.__dict__.get("_run_timed_command")
                if self._verify_hook_original is None:
                    # Class method: bind once for the dispatcher fallback path.
                    self._verify_hook_original = verifier._run_timed_command
                self._verify_hook_installed = installed
                setattr(verifier, "_run_timed_command", installed)
            self._verify_hook_users += 1

        previous = getattr(self._verify_hook_local, "on_process_start", None)
        self._verify_hook_local.on_process_start = on_process_start
        try:
            return verifier.verify_task(task, worktree, base_ref, env=task_env)
        finally:
            if previous is None:
                try:
                    del self._verify_hook_local.on_process_start
                except AttributeError:
                    pass
            else:
                self._verify_hook_local.on_process_start = previous
            with self._verify_hook_lock:
                self._verify_hook_users -= 1
                if self._verify_hook_users == 0:
                    if verifier.__dict__.get("_run_timed_command") is self._verify_hook_installed:
                        if self._verify_hook_had_instance:
                            assert self._verify_hook_original is not None
                            setattr(
                                verifier, "_run_timed_command", self._verify_hook_original
                            )
                        else:
                            delattr(verifier, "_run_timed_command")
                    self._verify_hook_original = None
                    self._verify_hook_installed = None
                    self._verify_hook_had_instance = False

    def _remember_memory_peak(self, observed_peaks: list[float]) -> None:
        if not observed_peaks:
            return
        observed = max(observed_peaks)
        with self._memory_estimate_lock:
            current = self.current_memory_estimate()
            if observed > current:
                self.state.set_meta("resources.memory_per_agent_gb", observed)

    def _admission_decision(self) -> Admission | None:
        """Return can_start result, or None when free memory is unknown (parallel-limit only)."""
        free_gb = self._free_memory_gb()
        if free_gb is None:
            if not self._memory_fallback_logged:
                LOGGER.warning(
                    "Free memory unavailable; using max_parallel_agents=%s only",
                    self.config.execution.max_parallel_agents,
                )
                self._memory_fallback_logged = True
            return None
        return can_start(
            project=str(self.repo_root.resolve()),
            free_gb=free_gb,
            estimate_gb=self.current_memory_estimate(),
            min_free_gb=self.config.execution.min_free_memory_gb,
            max_parallel_agents=max(1, self.config.execution.max_parallel_agents),
            ledger=self._ledger.snapshot(),
            now=datetime.now(timezone.utc),
        )

    def _set_ready_waiting(self, ready_tasks: int) -> None:
        self._ledger.set_waiting(self.repo_root, os.getpid(), ready_tasks)

    def _append_event(self, sprint_id: str, text: str) -> None:
        append_event(self.repo_root, sprint_id, text)

    def _set_task_status(
        self,
        task: TaskSpec,
        runtime,
        status: TaskStatus,
        *,
        provider: str | None = None,
    ) -> None:
        runtime.status = status
        if status in _WAITING_EVENT_STATUSES:
            label = provider or runtime.waiting_provider or "unknown"
            self._append_event(task.sprint, f"{task.id} {status.value} ({label})")
        else:
            self._append_event(task.sprint, f"{task.id} {status.value}")

    @staticmethod
    def _review_finding_count(output: str) -> int:
        count = 0
        seen_verdict = False
        for line in output.splitlines():
            stripped = line.strip()
            if stripped.startswith("VERDICT:"):
                seen_verdict = True
                continue
            if not seen_verdict or not stripped or stripped.startswith("UI_CHECK:"):
                continue
            count += 1
        return count

    def _stop_request_path(self) -> Path:
        return self._runtime_root / "stop-request"

    def _stop_request_for_sprint(self, sprint_id: str) -> bool:
        path = self._stop_request_path()
        if not path.is_file():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return False
        if not isinstance(payload, dict):
            return False
        return payload.get("sprint") == sprint_id

    def _record_stop_pause(self, sprint: SprintSpec, stage: int, tasks: list[TaskSpec]) -> None:
        task_ids = sorted(
            task.id
            for task in tasks
            if self.state.get(task.id).status not in {TaskStatus.PASSED, TaskStatus.DONE}
        )
        self.state.set_meta(f"{sprint.id}.run_status", TaskStatus.PENDING.value)
        self.state.save()
        self.failures.record(
            FailureRecord(
                sprint=sprint.id,
                kind="STOP_REQUEST",
                stage=stage,
                task_ids=task_ids,
                message=(
                    "Stop requested. Running agents finished; remaining work was preserved. "
                    "Resume when ready."
                ),
                suggested_command=f"harness resume {sprint.id}",
            )
        )
        self._append_event(sprint.id, "stop requested")
        self._stop_request_path().unlink(missing_ok=True)
        print(
            f"Stop requested for {sprint.id}; paused with resumable state. "
            f"Continue with: harness resume {sprint.id}"
        )

    def _record_restart_on_new_code(self, sprint: SprintSpec, stage: int) -> None:
        self.state.set_meta(f"{sprint.id}.run_status", TaskStatus.PENDING.value)
        self.state.save()
        self.failures.record(
            FailureRecord(
                sprint=sprint.id,
                kind="RESTART_ON_NEW_CODE",
                stage=stage,
                task_ids=[],
                message=(
                    "Stage integrated successfully. Pausing so the run can continue on a "
                    "frozen copy of the new harness code."
                ),
                suggested_command=f"harness resume {sprint.id}",
            )
        )
        self._append_event(sprint.id, "restart on new code")
        print(
            f"Self-hosting: stage {stage} integrated; restarting on new code. "
            f"Continue with: harness resume {sprint.id}"
        )

    def _submit_admitted_task(
        self,
        pool: concurrent.futures.ThreadPoolExecutor,
        task: TaskSpec,
        current_commit: str,
        futures: dict[concurrent.futures.Future, TaskSpec],
    ) -> None:
        estimate = self.current_memory_estimate()
        agent_id = self._ledger.register_agent(
            self.repo_root, task.id, os.getpid(), estimate
        )

        def run() -> TaskOutcome:
            try:
                return self._run_task(task, current_commit)
            finally:
                if agent_id:
                    self._ledger.unregister_agent(agent_id)

        futures[pool.submit(run)] = task

    @staticmethod
    def _scope_prefix(pattern: str) -> str:
        pattern = pattern.replace("\\", "/")
        wildcard_positions = [i for i in (pattern.find("*"), pattern.find("?"), pattern.find("[")) if i >= 0]
        cut = min(wildcard_positions) if wildcard_positions else len(pattern)
        return pattern[:cut].rstrip("/")

    @classmethod
    def _tasks_may_conflict(cls, left: TaskSpec, right: TaskSpec) -> bool:
        if not left.allowed_paths or not right.allowed_paths:
            return True
        for lp in left.allowed_paths:
            for rp in right.allowed_paths:
                lprefix = cls._scope_prefix(lp)
                rprefix = cls._scope_prefix(rp)
                if not lprefix or not rprefix:
                    return True
                if lprefix == rprefix or lprefix.startswith(rprefix + "/") or rprefix.startswith(lprefix + "/"):
                    return True
        return False

    @classmethod
    def _assert_parallel_safe(cls, tasks: list[TaskSpec], stage: int) -> None:
        conflicts: list[str] = []
        for i, left in enumerate(tasks):
            for right in tasks[i + 1:]:
                if cls._tasks_may_conflict(left, right):
                    conflicts.append(f"{left.id}<->{right.id}")
        if conflicts:
            raise RuntimeError(
                f"Stage {stage} contains tasks with overlapping/unknown allowed_paths: "
                + ", ".join(conflicts)
                + ". Put conflicting tasks in separate stages or narrow their allowed_paths."
            )

    def _defaults(self, task: TaskSpec, *, model_class_override: str | None = None) -> tuple[int, int]:
        # A focused/manual model override is intentionally one-shot so an
        # expensive escalation model cannot silently consume the full automatic
        # retry sequence. Automatic task execution uses the configured model
        # sequence length unless the task explicitly caps it with max_attempts.
        if model_class_override is not None:
            attempts = 1
        elif task.max_attempts is not None:
            attempts = task.max_attempts
        elif getattr(self.runner, "manages_model_routing", False):
            attempts = self.runner.max_implementation_attempts(task)
        elif self.config.retry.sequence:
            attempts = len(self.config.retry.sequence)
        else:
            attempts = self.config.execution.default_max_attempts
        timeout = task.timeout_minutes or self.config.execution.default_timeout_minutes
        return attempts, timeout

    def _implementation_model_class(
        self, task: TaskSpec, attempt_number: int, *, model_class_override: str | None = None
    ) -> str:
        if model_class_override is not None:
            return model_class_override
        if self.config.retry.sequence:
            index = attempt_number - 1
            if index >= len(self.config.retry.sequence):
                # Validation prevents this for configured tasks; keep a safe
                # deterministic fallback for direct/internal callers.
                return self.config.retry.sequence[-1]
            return self.config.retry.sequence[index]
        return task.model_class

    def _dependencies_done(self, task: TaskSpec) -> bool:
        return all(self.state.get(dep).status == TaskStatus.DONE for dep in task.depends_on)

    def _record_task_failure(self, task: TaskSpec, error: str) -> None:
        self.failures.record(
            FailureRecord(
                sprint=task.sprint,
                kind="TASK_BLOCKED",
                stage=task.stage,
                task_ids=[task.id],
                message=(
                    error + "\nIf a focused retry still cannot fit the current plan, analyze it with: "
                    f"harness plan-change propose {task.sprint} --task {task.id}"
                ),
                suggested_command=(
                    f"harness rerun-blocker {task.sprint} --model-class escalation"
                ),
            )
        )

    def _record_task_failures(self, sprint: SprintSpec, stage: int, outcomes: list[TaskOutcome]) -> None:
        task_ids = [outcome.task_id for outcome in outcomes]
        details = "; ".join(
            f"{outcome.task_id}: {(outcome.error or 'failed').splitlines()[0][:240]}"
            for outcome in outcomes
        )
        details += (
            "\nIf a blocked task proves to be a plan gap, run `plan-change propose` for that task "
            "instead of repeatedly expanding retry budget."
        )
        if len(task_ids) == 1:
            suggestion = f"harness rerun-blocker {sprint.id} --model-class escalation"
        else:
            suggestion = (
                f"harness rerun-blocker {sprint.id} --task <TASK_ID> "
                "--model-class escalation"
            )
        self.failures.record(
            FailureRecord(
                sprint=sprint.id,
                kind="TASK_BLOCKED",
                stage=stage,
                task_ids=task_ids,
                message=details,
                suggested_command=suggestion,
            )
        )

    def _record_stage_failure(self, sprint: SprintSpec, stage: int, tasks: list[TaskSpec], error: str) -> None:
        self.failures.record(
            FailureRecord(
                sprint=sprint.id,
                kind="STAGE_BARRIER",
                stage=stage,
                task_ids=[task.id for task in tasks],
                message=error,
                suggested_command=f"harness rerun-blocker {sprint.id}",
            )
        )

    def _record_quota_pause(self, sprint: SprintSpec, stage: int, outcomes: list[TaskOutcome]) -> None:
        task_ids = sorted(outcome.task_id for outcome in outcomes)
        self.state.set_meta(f"{sprint.id}.run_status", TaskStatus.WAITING_QUOTA.value)
        providers = ", ".join(sorted({outcome.provider or "unknown" for outcome in outcomes}))
        self.failures.record(
            FailureRecord(
                sprint=sprint.id,
                kind="QUOTA_WAIT",
                stage=stage,
                task_ids=task_ids,
                message=(
                    f"Provider usage quota is exhausted ({providers}). Task state/worktrees were preserved and "
                    "quota pauses did not consume retry budget."
                ),
                suggested_command=f"harness resume {sprint.id}",
            )
        )


    def _record_capacity_pause(self, sprint: SprintSpec, stage: int, outcomes: list[TaskOutcome]) -> None:
        task_ids = sorted(outcome.task_id for outcome in outcomes)
        providers = ", ".join(sorted({outcome.provider or "unknown" for outcome in outcomes}))
        # Resource launch requeues reuse the capacity-wait loop for later retry, but they are
        # a machine-resource problem — never stamp WAITING_PROVIDER_ERROR (S8-T14 Decision 5).
        if outcomes and all(outcome.resource_paused for outcome in outcomes):
            self.state.set_meta(f"{sprint.id}.run_status", TaskStatus.PENDING.value)
            causes = "out of resources"
            headline = "Agent launch is waiting"
        else:
            status_source = next(
                (outcome for outcome in outcomes if not outcome.resource_paused),
                outcomes[0],
            )
            first_status = status_source.waiting_status or TaskStatus.WAITING_PROVIDER_ERROR
            self.state.set_meta(f"{sprint.id}.run_status", first_status.value)
            causes = ", ".join(
                sorted({
                    self._waiting_label(
                        outcome.waiting_status, resource_paused=outcome.resource_paused
                    )
                    for outcome in outcomes
                })
            )
            headline = "Agent call is waiting"
        self.failures.record(
            FailureRecord(
                sprint=sprint.id,
                kind="CAPACITY_WAIT",
                stage=stage,
                task_ids=task_ids,
                message=(
                    f"{headline} ({causes}; {providers}). Task state/worktrees were preserved and "
                    "the pause did not consume retry budget."
                ),
                suggested_command=f"harness resume {sprint.id}",
            )
        )

    def _setup_worktree(self, task: TaskSpec, worktree: Path, env: dict[str, str]) -> tuple[bool, str | None]:
        commands = self.config.worktree.setup_commands
        if not commands:
            return True, None

        outputs: list[str] = []
        timeout_seconds = max(1, self.config.worktree.setup_timeout_minutes) * 60
        for command in commands:
            try:
                proc = subprocess.run(
                    command,
                    cwd=worktree,
                    text=True, encoding="utf-8", errors="replace",
                    shell=True,
                    capture_output=True,
                    timeout=timeout_seconds,
                    env=env,
                )
                outputs.append(f"$ {command}\nexit={proc.returncode}\n{proc.stdout}{proc.stderr}".rstrip())
                if proc.returncode != 0:
                    error = f"Worktree setup command failed: {command}"
                    write_log(self.verifier.log_dir, f"{task.id}-worktree-setup.log", "\n\n".join(outputs))
                    return False, error
            except subprocess.TimeoutExpired as exc:
                stdout = _subprocess_text(exc.stdout)
                stderr = _subprocess_text(exc.stderr)
                rendered = stdout + (f"\n{stderr}" if stderr else "")
                outputs.append(f"$ {command}\nTIMEOUT\n{rendered}".rstrip())
                write_log(self.verifier.log_dir, f"{task.id}-worktree-setup.log", "\n\n".join(outputs))
                return False, f"Worktree setup timed out: {command}"

        write_log(self.verifier.log_dir, f"{task.id}-worktree-setup.log", "\n\n".join(outputs))
        return True, None

    def _block_task(self, task: TaskSpec, error: str, worktree: Path | None = None) -> TaskOutcome:
        runtime = self.state.get(task.id)
        self._set_task_status(task, runtime, TaskStatus.BLOCKED)
        runtime.finished_at = utc_now()
        runtime.last_error = error
        self.state.save()
        if worktree is not None:
            self.environment.teardown_task(task, worktree, blocked=True)
        self._record_task_failure(task, error)
        return TaskOutcome(task.id, False, error)

    @staticmethod
    def _is_launch_failure(result: AgentResult) -> bool:
        return bool(getattr(result, "launch_failure", False))

    @staticmethod
    def _is_resource_launch_failure(result: AgentResult) -> bool:
        return bool(getattr(result, "resource_launch_failure", False))

    def _launch_fallback_available(self, task: TaskSpec, failed_provider: str) -> bool:
        sprint_task = f"{task.sprint}.{task.id}"
        if failed_provider == "codex":
            return (
                self.config.cursor.enabled
                and not bool(self.state.get_meta(f"{sprint_task}.provider.cursor.disabled"))
            )
        if failed_provider == "cursor":
            return (
                self.config.codex.enabled
                and not bool(self.state.get_meta(f"{sprint_task}.provider.codex.disabled"))
            )
        return False

    def _disable_provider_for_launch_failure(self, task: TaskSpec, provider: str) -> None:
        if provider == "codex":
            key = f"{task.sprint}.{task.id}.provider.codex.disabled"
        elif provider == "cursor":
            key = f"{task.sprint}.{task.id}.provider.cursor.disabled"
        else:
            return
        self.state.set_meta(key, "launch_failure")

    def _block_launch_failure(
        self, task: TaskSpec, error: str, worktree: Path | None = None
    ) -> TaskOutcome:
        # Keep the setup-machine hint on the first line: `_record_task_failures`
        # stores only `splitlines()[0]` of each outcome error. Also record here so
        # `harness run-task` (which does not call `_record_task_failures`) still
        # leaves a FailureStore entry for `harness failure`.
        message = f"{error} — check the program with harness setup-machine"
        runtime = self.state.get(task.id)
        self._set_task_status(task, runtime, TaskStatus.BLOCKED)
        runtime.finished_at = utc_now()
        runtime.last_error = message
        runtime.waiting_phase = None
        runtime.waiting_provider = None
        self.state.save()
        if worktree is not None:
            self.environment.teardown_task(task, worktree, blocked=True)
        self._record_task_failure(task, message)
        return TaskOutcome(task.id, False, message)

    def _requeue_resource_launch_failure(
        self,
        task: TaskSpec,
        worktree: Path,
        result: AgentResult,
        *,
        phase: str,
        decrement_attempt: bool,
    ) -> TaskOutcome:
        runtime = self.state.get(task.id)
        if decrement_attempt and runtime.attempt > 0:
            runtime.attempt -= 1
        runtime.resource_launch_failures += 1
        error = result.error or f"{result.provider} could not start: the computer is out of resources"
        if runtime.resource_launch_failures >= RESOURCE_LAUNCH_FAILURE_LIMIT:
            self.state.save()
            return self._block_task(task, error, worktree)
        # Back on the stage queue; capacity_paused retries after the interval (S8-T6 refines timing).
        # Keep waiting_phase so a review-time resource exit resumes review, not a new implement.
        self._set_task_status(task, runtime, TaskStatus.PENDING)
        runtime.waiting_phase = phase
        runtime.waiting_provider = None
        runtime.last_error = error
        self.state.save()
        return TaskOutcome(
            task.id,
            False,
            error,
            capacity_paused=True,
            resource_paused=True,
            provider=result.provider,
        )

    def _pause_for_quota(
        self, task: TaskSpec, worktree: Path, *, phase: str, decrement_attempt: bool,
        provider: str,
    ) -> TaskOutcome:
        runtime = self.state.get(task.id)
        if decrement_attempt and runtime.attempt > 0:
            runtime.attempt -= 1
        runtime.waiting_phase = phase
        runtime.waiting_provider = provider
        self._set_task_status(task, runtime, TaskStatus.WAITING_QUOTA, provider=provider)
        runtime.last_error = f"{provider} usage quota exhausted during {phase}"
        self.state.save()
        if self.config.quota.teardown_environment_while_waiting:
            self.environment.teardown_task(task, worktree, blocked=True)
        return TaskOutcome(
            task.id, False, runtime.last_error, quota_paused=True,
            waiting_status=runtime.status, provider=provider,
        )


    @staticmethod
    def _waiting_status(cause: str | None) -> TaskStatus:
        return {
            "network": TaskStatus.WAITING_NETWORK,
            "provider_busy": TaskStatus.WAITING_PROVIDER_BUSY,
            "provider_error": TaskStatus.WAITING_PROVIDER_ERROR,
            "timeout": TaskStatus.WAITING_TIMEOUT,
        }.get(cause or "provider_error", TaskStatus.WAITING_PROVIDER_ERROR)

    @staticmethod
    def _waiting_label(status: TaskStatus | None, *, resource_paused: bool = False) -> str:
        if resource_paused:
            return "out of resources"
        if status is None:
            return "provider error"
        return {
            TaskStatus.WAITING_NETWORK: "network",
            TaskStatus.WAITING_PROVIDER_BUSY: "provider busy",
            TaskStatus.WAITING_PROVIDER_ERROR: "provider error",
            TaskStatus.WAITING_TIMEOUT: "timeout",
        }.get(status, "provider error")

    def _pause_for_capacity(
        self, task: TaskSpec, worktree: Path, *, phase: str, decrement_attempt: bool,
        result: AgentResult,
    ) -> TaskOutcome:
        runtime = self.state.get(task.id)
        if decrement_attempt and runtime.attempt > 0:
            runtime.attempt -= 1
        cause = result.waiting_cause or ("timeout" if result.timed_out or result.stalled else (
            "provider_busy" if result.capacity_exhausted else "provider_error"
        ))
        runtime.waiting_phase = phase
        runtime.waiting_provider = result.provider
        self._set_task_status(
            task, runtime, self._waiting_status(cause), provider=result.provider
        )
        runtime.last_error = f"{result.provider} {self._waiting_label(runtime.status)} during {phase}"
        self.state.save()
        if self.config.capacity.teardown_environment_while_waiting:
            self.environment.teardown_task(task, worktree, blocked=True)
        return TaskOutcome(
            task.id, False, runtime.last_error, capacity_paused=True,
            waiting_status=runtime.status, provider=result.provider,
        )

    @staticmethod
    def _clear_phase_stalls(runtime, phase: str) -> None:
        for key in [key for key in runtime.stall_counts if key.startswith(f"{phase}:")]:
            del runtime.stall_counts[key]

    @staticmethod
    def _verification_commit_meta_key(task_id: str) -> str:
        return f"tasks.{task_id}.verification_commit"

    @staticmethod
    def _verification_tree_meta_key(task_id: str) -> str:
        return f"tasks.{task_id}.verification_fingerprint"

    @staticmethod
    def _worktree_head(worktree: Path) -> str:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=True,
        )
        return result.stdout.strip()

    @staticmethod
    def _worktree_porcelain(worktree: Path) -> str:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=True,
        )
        return result.stdout

    def _worktree_verification_fingerprint(self, worktree: Path, base_ref: str) -> str:
        porcelain = self._worktree_porcelain(worktree)
        hasher = hashlib.sha256()
        hasher.update(porcelain.encode("utf-8"))
        for path in sorted(self.worktrees.changed_files(worktree, base_ref)):
            file_path = worktree / path
            if file_path.is_file():
                hasher.update(path.encode("utf-8"))
                hasher.update(file_path.read_bytes())
        return hasher.hexdigest()

    def _record_verification_worktree(self, task_id: str, worktree: Path, base_ref: str) -> None:
        commit_key = self._verification_commit_meta_key(task_id)
        if not self.state.get_meta(commit_key):
            self.state.set_meta(commit_key, self._worktree_head(worktree))
        self.state.set_meta(
            self._verification_tree_meta_key(task_id),
            self._worktree_verification_fingerprint(worktree, base_ref),
        )

    def _review_resume_skips_verification(
        self, task_id: str, worktree: Path, base_ref: str,
    ) -> bool:
        recorded_commit = self.state.get_meta(self._verification_commit_meta_key(task_id))
        if not isinstance(recorded_commit, str) or not recorded_commit:
            return False
        if self._worktree_head(worktree) != recorded_commit:
            return False
        recorded_fingerprint = self.state.get_meta(self._verification_tree_meta_key(task_id))
        if not isinstance(recorded_fingerprint, str):
            return False
        return self._worktree_verification_fingerprint(worktree, base_ref) == recorded_fingerprint

    def _handle_agent_wait(
        self, task: TaskSpec, worktree: Path, result: AgentResult, *, phase: str,
        decrement_attempt: bool,
    ) -> TaskOutcome | None:
        if result.quota_exhausted:
            self._clear_phase_stalls(self.state.get(task.id), phase)
            return self._pause_for_quota(
                task, worktree, phase=phase, decrement_attempt=decrement_attempt,
                provider=result.provider,
            )
        cause = result.waiting_cause or (
            "timeout" if result.timed_out or result.stalled else
            ("provider_busy" if result.capacity_exhausted else
             ("provider_error" if result.transient_error else None))
        )
        if cause is None:
            self._clear_phase_stalls(self.state.get(task.id), phase)
            return None
        runtime = self.state.get(task.id)
        if cause == "timeout":
            key = f"{phase}:{result.provider}"
            runtime.stall_counts[key] = runtime.stall_counts.get(key, 0) + 1
            if runtime.stall_counts[key] >= 3:
                if decrement_attempt and runtime.attempt > 0:
                    runtime.attempt -= 1
                    decrement_attempt = False
                if not self.runner.fallback_after_stalls(task, result.provider, phase):
                    self.state.save()
                    return self._block_task(task, "stalled 3 times", worktree)
        else:
            self._clear_phase_stalls(runtime, phase)
        return self._pause_for_capacity(
            task, worktree, phase=phase, decrement_attempt=decrement_attempt, result=result,
        )

    def _verify_review_commit(
        self,
        task: TaskSpec,
        runtime,
        worktree: Path,
        base_ref: str,
        timeout: int,
        task_env: dict[str, str],
        code_map_offered: bool,
        observed_peaks: list[float],
        *,
        resume_review: bool = False,
    ) -> TaskOutcome | None:
        skip_verification = resume_review and self._review_resume_skips_verification(
            task.id, worktree, base_ref,
        )
        if skip_verification:
            verification_summary = "Task verification passed before the reviewer wait."
            review_required = True
        else:
            self._set_task_status(task, runtime, TaskStatus.CHECKING)
            runtime.waiting_phase = None
            runtime.waiting_provider = None
            self.state.save()
            with self._process_sampler(observed_peaks) as on_process_start:
                verification = self._verify_with_process_hook(
                    task, worktree, base_ref, task_env, on_process_start
                )
            if not verification.ok:
                self._set_task_status(task, runtime, TaskStatus.RETRY)
                runtime.last_error = verification.summary
                self.state.save()
                return None
            self._record_verification_worktree(task.id, worktree, base_ref)
            verification_summary = verification.summary
            review_required = task.review or self.verifier.requires_review(verification.changed_files)

        if review_required:
            self._set_task_status(task, runtime, TaskStatus.REVIEWING)
            self.state.save()
            with self._process_sampler(observed_peaks) as on_process_start:
                review = self.runner.review(
                    task, worktree, base_ref, verification_summary, timeout,
                    env=task_env, code_map_offered=code_map_offered,
                    on_process_start=on_process_start,
                )
            if self._is_resource_launch_failure(review):
                return self._requeue_resource_launch_failure(
                    task, worktree, review, phase="review", decrement_attempt=False
                )
            if self._is_launch_failure(review):
                error = review.error or f"{review.provider} could not start"
                if self._launch_fallback_available(task, review.provider):
                    self._disable_provider_for_launch_failure(task, review.provider)
                    # Re-enter review on the alternate provider without another implement call.
                    with self._process_sampler(observed_peaks) as on_process_start:
                        review = self.runner.review(
                            task, worktree, base_ref, verification_summary, timeout,
                            env=task_env, code_map_offered=code_map_offered,
                            on_process_start=on_process_start,
                        )
                    if self._is_launch_failure(review) or self._is_resource_launch_failure(review):
                        if self._is_resource_launch_failure(review):
                            return self._requeue_resource_launch_failure(
                                task, worktree, review, phase="review", decrement_attempt=False
                            )
                        return self._block_launch_failure(
                            task, review.error or error, worktree
                        )
                else:
                    return self._block_launch_failure(task, error, worktree)
            paused = self._handle_agent_wait(
                task, worktree, review, phase="review", decrement_attempt=False
            )
            if paused is not None:
                return paused
            verdict_pass = review.ok and any(
                line.strip() == "VERDICT: PASS" for line in review.output.splitlines()
            )
            findings = self._review_finding_count(review.output or "")
            verdict_label = "PASS" if verdict_pass else "FAIL"
            self._append_event(
                task.sprint,
                f"{task.id} review {verdict_label} ({findings} findings)",
            )
            if not verdict_pass:
                feedback = review.output or review.error or "Reviewer failed without feedback"
                self._set_task_status(task, runtime, TaskStatus.RETRY)
                runtime.review_feedback = feedback
                runtime.last_error = feedback
                self.state.save()
                return None

        try:
            runtime.commit = self.worktrees.commit_all(worktree, task.id)
        except Exception as exc:
            self._set_task_status(task, runtime, TaskStatus.RETRY)
            runtime.last_error = f"Commit failed: {exc}"
            self.state.save()
            return None

        self._set_task_status(task, runtime, TaskStatus.PASSED)
        runtime.last_error = None
        runtime.review_feedback = None
        runtime.waiting_phase = None
        runtime.waiting_provider = None
        self._clear_phase_stalls(runtime, "review")
        self.state.save()
        self.environment.teardown_task(task, worktree, blocked=False)
        return TaskOutcome(task.id, True)

    def _run_task(
        self,
        task: TaskSpec,
        base_ref: str,
        *,
        model_class_override: str | None = None,
    ) -> TaskOutcome:
        observed_peaks: list[float] = []
        try:
            return self._run_task_inner(
                task,
                base_ref,
                observed_peaks,
                model_class_override=model_class_override,
            )
        finally:
            self._remember_memory_peak(observed_peaks)

    def _run_task_inner(
        self,
        task: TaskSpec,
        base_ref: str,
        observed_peaks: list[float],
        *,
        model_class_override: str | None = None,
    ) -> TaskOutcome:
        runtime = self.state.get(task.id)
        max_attempts, timeout = self._defaults(task, model_class_override=model_class_override)

        if runtime.status in {TaskStatus.DONE, TaskStatus.PASSED}:
            return TaskOutcome(task.id, True)

        worktree: Path | None = None
        try:
            created = False
            if not runtime.worktree or not Path(runtime.worktree).exists():
                branch, worktree = self.worktrees.create(task.id, base_ref)
                created = True
                runtime.branch = branch
                runtime.worktree = str(worktree)
                runtime.base_ref = base_ref
                self.state.save()
            else:
                existing = Path(runtime.worktree)
                # create() reuses only when this path is registered on the task branch;
                # otherwise it replaces an unregistered folder with a fresh worktree.
                prior_branch = self.worktrees._registered_branch(existing)
                branch, worktree = self.worktrees.create(task.id, base_ref)
                if prior_branch == branch:
                    # An existing registered worktree is diffed against the commit it was
                    # created from. Using a newer stage base would report every later
                    # integration commit as a change by this task.
                    if runtime.base_ref:
                        base_ref = runtime.base_ref
                else:
                    created = True
                    runtime.branch = branch
                    runtime.worktree = str(worktree)
                    runtime.base_ref = base_ref
                    self.state.save()
                    LOGGER.info(
                        "worktree for %s was not registered; created a fresh one",
                        task.id,
                    )

            task_env = self.environment.task_env(task, worktree)
            if created:
                setup_ok, setup_error = self._setup_worktree(task, worktree, task_env)
                if not setup_ok:
                    return self._block_task(task, setup_error or "worktree setup failed", worktree)

            env_result = self.environment.prepare_task(task, worktree)
            if not env_result.ok:
                return self._block_task(task, env_result.message, worktree)
        except Exception as exc:
            return self._block_task(task, str(exc), worktree)

        assert worktree is not None
        task_env = self.environment.task_env(task, worktree)
        self.code_maps.ensure_project_map(base_ref, env=task_env)
        code_map_offered = self.code_maps.offer_to_worktree(task, worktree)

        # If quota/capacity was exhausted only at review time, keep the already-written
        # implementation and resume from verification/review without another
        # coding-model call.
        waiting_statuses = {
            TaskStatus.WAITING_QUOTA, TaskStatus.WAITING_NETWORK,
            TaskStatus.WAITING_PROVIDER_BUSY, TaskStatus.WAITING_PROVIDER_ERROR,
            TaskStatus.WAITING_TIMEOUT,
        }
        # PENDING + waiting_phase=review is the resource-launch requeue (not a provider wait).
        resume_review = runtime.waiting_phase == "review" and (
            runtime.status in waiting_statuses or runtime.status == TaskStatus.PENDING
        )
        if resume_review:
            resumed = self._verify_review_commit(
                task, runtime, worktree, base_ref, timeout, task_env, code_map_offered,
                observed_peaks,
                resume_review=True,
            )
            if resumed is not None:
                return resumed

        previous_failure = runtime.last_error or runtime.review_feedback
        while runtime.attempt < max_attempts and (
            not getattr(self.runner, "manages_model_routing", False)
            or self.runner.has_implementation_budget(task, model_class_override=model_class_override)
        ):
            runtime.attempt += 1
            self._set_task_status(task, runtime, TaskStatus.IMPLEMENTING)
            runtime.waiting_phase = None
            runtime.waiting_provider = None
            runtime.started_at = runtime.started_at or utc_now()
            runtime.last_error = None
            self.state.save()

            attempt_model_class = (
                model_class_override if getattr(self.runner, "manages_model_routing", False)
                else self._implementation_model_class(task, runtime.attempt, model_class_override=model_class_override)
            )
            with self._process_sampler(observed_peaks) as on_process_start:
                result = self.runner.implement(
                    task,
                    worktree,
                    timeout,
                    runtime.attempt,
                    previous_failure,
                    env=task_env,
                    model_class_override=attempt_model_class,
                    code_map_offered=code_map_offered,
                    on_process_start=on_process_start,
                )
            if self._is_resource_launch_failure(result):
                return self._requeue_resource_launch_failure(
                    task, worktree, result, phase="implement", decrement_attempt=True
                )
            if self._is_launch_failure(result):
                error = result.error or f"{result.provider} could not start"
                if self._launch_fallback_available(task, result.provider):
                    self._disable_provider_for_launch_failure(task, result.provider)
                    if runtime.attempt > 0:
                        runtime.attempt -= 1
                    runtime.last_error = error
                    self.state.save()
                    previous_failure = error
                    continue
                if runtime.attempt > 0:
                    runtime.attempt -= 1
                self.state.save()
                return self._block_launch_failure(task, error, worktree)
            runtime.resource_launch_failures = 0
            paused = self._handle_agent_wait(
                task, worktree, result, phase="implement", decrement_attempt=True
            )
            if paused is not None:
                return paused
            if not result.ok:
                previous_failure = result.error or "Agent execution failed"
                self._set_task_status(task, runtime, TaskStatus.RETRY)
                runtime.last_error = previous_failure
                self.state.save()
                continue

            finished = self._verify_review_commit(
                task, runtime, worktree, base_ref, timeout, task_env, code_map_offered,
                observed_peaks,
            )
            if finished is not None:
                return finished
            previous_failure = runtime.last_error or runtime.review_feedback or "Verification/review failed"

        return self._block_task(task, previous_failure or "Attempt budget exhausted", worktree)

    def _rollback_stage(self, checkpoint: str) -> str | None:
        try:
            self.worktrees.rollback_integration(checkpoint)
            return None
        except Exception as exc:
            return str(exc)

    def _integrate_stage(self, sprint: SprintSpec, stage: int, tasks: list[TaskSpec], checkpoint: str) -> bool:
        self._merge_in_progress = (sprint.id, stage)
        self._append_event(sprint.id, f"stage {stage} merge started")
        for task in tasks:
            self._set_task_status(task, self.state.get(task.id), TaskStatus.MERGING)
        self.state.save()
        try:
            ok = self._integrate_stage_inner(sprint, stage, tasks, checkpoint)
            self._append_event(
                sprint.id,
                f"stage {stage} merged" if ok else f"stage {stage} merge failed",
            )
            return ok
        finally:
            self._merge_in_progress = None

    def _integrate_stage_inner(self, sprint: SprintSpec, stage: int, tasks: list[TaskSpec], checkpoint: str) -> bool:
        try:
            for task in tasks:
                runtime = self.state.get(task.id)
                if runtime.status != TaskStatus.MERGING or not runtime.branch:
                    raise GitError(f"{task.id} is not MERGING and cannot be integrated")
                self.worktrees.merge_branch(runtime.branch, task.id)

            stage_env = self.environment.stage_env(sprint.id, stage, self.repo_root)
            stage_check = self.verifier.verify_stage(
                self.repo_root,
                sprint.stage_verification.get(stage, []),
                f"{sprint.id}-stage-{stage}",
                env=stage_env,
            )
            if stage_check.ok and not self.worktrees.is_clean():
                stage_check.ok = False
                stage_check.failures.append("Stage verification left the integration checkout dirty")

            if not stage_check.ok:
                rollback_error = self._rollback_stage(checkpoint)
                detail = "; ".join(stage_check.failures) or "stage verification command failed"
                error = (
                    f"Stage integration verification failed: {detail}. "
                    "Integration checkout rolled back to the pre-stage checkpoint."
                )
                if rollback_error:
                    error += f" ROLLBACK FAILED: {rollback_error}"
                self._record_stage_failure(sprint, stage, tasks, error)
                for task in tasks:
                    self._set_task_status(task, self.state.get(task.id), TaskStatus.PASSED)
                self.state.save()
                return False
        except Exception as exc:
            rollback_error = self._rollback_stage(checkpoint)
            error = f"Stage integration failed: {exc}; integration checkout rolled back to the pre-stage checkpoint."
            if rollback_error:
                error += f" ROLLBACK FAILED: {rollback_error}"
            self._record_stage_failure(sprint, stage, tasks, error)
            for task in tasks:
                self._set_task_status(task, self.state.get(task.id), TaskStatus.PASSED)
            self.state.save()
            return False

        finished_at = utc_now()
        for task in tasks:
            runtime = self.state.get(task.id)
            self._set_task_status(task, runtime, TaskStatus.DONE)
            runtime.finished_at = finished_at
        self.state.save()

        cleanup_warnings: list[str] = []
        cleaned_any = False
        for task in tasks:
            runtime = self.state.get(task.id)
            if not self.config.execution.keep_successful_worktrees and runtime.worktree and runtime.branch:
                worktree_path = Path(runtime.worktree)
                try:
                    self.worktrees.remove(worktree_path, runtime.branch, delete_branch=True)
                except Exception as exc:
                    message = f"WARNING: failed to remove worktree {worktree_path}: {exc}"
                    cleanup_warnings.append(message)
                    print(message, file=sys.stderr)
                    continue
                runtime.worktree = None
                runtime.branch = None
                cleaned_any = True
        if cleaned_any:
            self.state.save()
        if cleanup_warnings:
            write_log(
                self.verifier.log_dir,
                f"{sprint.id}-stage-{stage}-worktree-cleanup.log",
                "\n".join(cleanup_warnings) + "\n",
            )
        self.failures.clear(sprint.id)
        return True

    def rerun_stage_barrier(self, sprint: SprintSpec, stage: int) -> bool:
        self.task_queue.merge_added_tasks_from_state(sprint)
        tasks = [task for task in sprint.tasks.values() if task.stage == stage]
        if not tasks:
            raise RuntimeError(f"Sprint {sprint.id} has no stage {stage}")
        not_verified = [
            task.id for task in tasks if self.state.get(task.id).status not in {TaskStatus.PASSED, TaskStatus.DONE}
        ]
        if not_verified:
            raise RuntimeError(f"Cannot rerun stage {stage}; tasks not PASSED/DONE: {', '.join(not_verified)}")
        pending = [task for task in tasks if self.state.get(task.id).status != TaskStatus.DONE]
        if not pending:
            self.failures.clear(sprint.id)
            return True
        if not self.worktrees.is_clean():
            raise RuntimeError("Integration checkout is dirty; cannot rerun stage barrier safely")
        checkpoint = self.worktrees.head_commit()
        ok = self._integrate_stage(sprint, stage, pending, checkpoint)
        if ok:
            # Merge finished: remaining queue entries cannot join this stage, but
            # auto/next must still be able to create a following stage before we
            # treat the sprint as complete (Decision 3/4).
            self._pickup_queue(sprint, current_stage=stage, merge_in_progress=True)
            self.finalize_completed_sprint(sprint)
        return ok

    def finalize_completed_sprint(self, sprint: SprintSpec, *, dry_run: bool = False) -> None:
        if self.task_queue.pending_entries(sprint.id):
            return
        if not sprint_is_complete(self.state, sprint):
            return
        if not dry_run and self.roadmap_path is not None:
            self.task_queue.record_added_tasks_to_repo(sprint, self.roadmap_path)
        self.state.set_meta(f"{sprint.id}.run_status", "COMPLETED")
        self.failures.clear(sprint.id)
        before_release = self.state.get_meta(f"{sprint.id}.release.status")
        ReleaseRunner(self.config, self.repo_root, self.verifier).maybe_run_after_sprint_complete(
            self.state, sprint.id, dry_run=dry_run
        )
        after_release = self.state.get_meta(f"{sprint.id}.release.status")
        if after_release is not None and after_release != before_release:
            self._append_event(sprint.id, f"release {after_release}")

    def _open_stages(self, sprint: SprintSpec, *, skip: set[int] | None = None) -> list[int]:
        skip = skip or set()
        return sorted(
            {
                task.stage
                for task in sprint.tasks.values()
                if self.state.get(task.id).status != TaskStatus.DONE and task.stage not in skip
            }
        )

    def _pickup_queue(
        self,
        sprint: SprintSpec,
        *,
        current_stage: int,
        merge_in_progress: bool,
    ) -> list[TaskSpec]:
        if self.roadmap is None:
            return []
        stage_tasks = [task for task in sprint.tasks.values() if task.stage == current_stage]
        added = self.task_queue.process_pending(
            sprint,
            self.roadmap,
            current_stage=current_stage,
            stage_tasks=stage_tasks,
            merge_in_progress=merge_in_progress,
            scheduler=self,
        )
        for task in added:
            self._append_event(sprint.id, f"added {task.id}")
        return added

    def run_sprint(self, sprint: SprintSpec, *, dry_run: bool = False) -> bool:
        poll_seconds = max(1, self.config.execution.watchdog_poll_seconds)
        dry_run_seen: set[int] = set()
        self._memory_fallback_logged = False
        try:
            return self._run_sprint_inner(sprint, dry_run=dry_run, poll_seconds=poll_seconds, dry_run_seen=dry_run_seen)
        finally:
            self._set_ready_waiting(0)

    def _run_sprint_inner(
        self,
        sprint: SprintSpec,
        *,
        dry_run: bool,
        poll_seconds: float,
        dry_run_seen: set[int],
    ) -> bool:
        while True:
            self.task_queue.merge_added_tasks_from_state(sprint)
            open_stages = self._open_stages(sprint, skip=dry_run_seen)
            if not open_stages:
                if not dry_run:
                    stages = sorted({task.stage for task in sprint.tasks.values()}) or [1]
                    self._pickup_queue(sprint, current_stage=stages[-1], merge_in_progress=True)
                    if self._open_stages(sprint):
                        continue
                break
            stage = open_stages[0]
            if not dry_run:
                self._pickup_queue(sprint, current_stage=stage, merge_in_progress=False)
            tasks = [task for task in sprint.tasks.values() if task.stage == stage]
            unfinished = [task for task in tasks if self.state.get(task.id).status != TaskStatus.DONE]
            if not unfinished:
                continue

            self._assert_parallel_safe(unfinished, stage)
            if dry_run:
                print(f"Stage {stage}: " + ", ".join(task.id for task in unfinished))
                dry_run_seen.add(stage)
                continue

            not_ready = [task.id for task in unfinished if not self._dependencies_done(task)]
            if not_ready:
                raise RuntimeError(
                    f"Stage {stage} contains tasks whose dependencies are not DONE: {', '.join(not_ready)}"
                )
            if not self.worktrees.is_clean():
                raise RuntimeError(
                    f"Integration checkout became dirty before stage {stage}; refusing to continue because "
                    "atomic rollback requires a clean pre-stage checkpoint."
                )

            integration_branch = self.worktrees.current_branch()
            current_commit = self.worktrees.head_commit()
            self.state.set_meta(f"{sprint.id}.integration_branch", integration_branch)
            self.state.set_meta(f"{sprint.id}.stage.{stage}.base_commit", current_commit)
            self.code_maps.ensure_project_map(current_commit)

            def poll_queue(
                pending: list[TaskSpec],
                active_unfinished: list[TaskSpec],
            ) -> None:
                added = self._pickup_queue(
                    sprint,
                    current_stage=stage,
                    merge_in_progress=self._merge_in_progress == (sprint.id, stage),
                )
                for task in added:
                    if task.stage != stage:
                        continue
                    if self.state.get(task.id).status == TaskStatus.DONE:
                        continue
                    if task not in active_unfinished:
                        active_unfinished.append(task)
                        pending.append(task)
                    self._assert_parallel_safe(
                        [t for t in active_unfinished if self.state.get(t.id).status != TaskStatus.DONE],
                        stage,
                    )

            max_workers = max(1, self.config.execution.max_parallel_agents)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                pending = list(unfinished)
                futures: dict[concurrent.futures.Future, TaskSpec] = {}
                outcomes: list[TaskOutcome] = []
                capacity_retries: dict[str, tuple[TaskSpec, float, TaskOutcome]] = {}
                retry_seconds = max(1.0, self.config.capacity.retry_interval_minutes * 60.0)
                stop_requested = False
                # Print/log each admission wait reason once until a task starts
                # or the reason text changes (exact match).
                last_memory_wait_reason: str | None = None

                def collect_completed(
                    completed: set[concurrent.futures.Future],
                ) -> None:
                    for future in completed:
                        task = futures.pop(future)
                        outcome = future.result()
                        if outcome.capacity_paused:
                            capacity_retries[outcome.task_id] = (
                                task,
                                self._clock() + retry_seconds,
                                outcome,
                            )
                        else:
                            outcomes.append(outcome)

                def try_start_one(task: TaskSpec) -> bool:
                    nonlocal stop_requested, last_memory_wait_reason
                    if self._stop_request_for_sprint(sprint.id):
                        stop_requested = True
                        return False
                    if len(futures) >= max_workers:
                        return False
                    admission = self._admission_decision()
                    if admission is None:
                        self._submit_admitted_task(pool, task, current_commit, futures)
                        last_memory_wait_reason = None
                        return True
                    if not admission.allowed:
                        if admission.reason != last_memory_wait_reason:
                            LOGGER.info("Admission wait: %s", admission.reason)
                            print(f"Waiting (memory): {admission.reason}")
                            last_memory_wait_reason = admission.reason
                        return False
                    self._submit_admitted_task(pool, task, current_commit, futures)
                    last_memory_wait_reason = None
                    return True

                def update_waiting_record() -> None:
                    now = self._clock()
                    ready = len(pending)
                    ready += sum(
                        1
                        for _, retry_at, _ in capacity_retries.values()
                        if retry_at <= now
                    )
                    self._set_ready_waiting(ready)

                while futures or pending or capacity_retries:
                    if self._stop_request_for_sprint(sprint.id):
                        stop_requested = True

                    # Harvest completions before scheduling retries. A future may have
                    # completed just after the previous FIRST_COMPLETED wait returned;
                    # it must not count as an active sibling that keeps retries in-stage.
                    already_completed = {future for future in futures if future.done()}
                    if already_completed:
                        collect_completed(already_completed)
                        if not stop_requested:
                            poll_queue(pending, unfinished)

                    if stop_requested:
                        if not futures:
                            break
                        done, _ = concurrent.futures.wait(
                            futures.keys(),
                            timeout=poll_seconds,
                            return_when=concurrent.futures.FIRST_COMPLETED,
                        )
                        if done:
                            collect_completed(done)
                        continue

                    # With no active sibling, every unresolved task is capacity-paused.
                    # Return to the CLI wait loop instead of sleeping inside the stage.
                    if not futures and not pending:
                        break

                    now = self._clock()
                    due = [
                        task_id
                        for task_id, (_, retry_at, _) in capacity_retries.items()
                        if retry_at <= now
                    ]
                    for task_id in due:
                        task, _, _ = capacity_retries[task_id]
                        if not try_start_one(task):
                            break
                        capacity_retries.pop(task_id)
                    if stop_requested:
                        continue

                    # Keep the backlog here, so due retries get the first free slot
                    # instead of sitting behind siblings in the executor's FIFO queue.
                    while pending:
                        task = pending[0]
                        if not try_start_one(task):
                            break
                        pending.pop(0)
                    if stop_requested:
                        continue

                    update_waiting_record()

                    wait_seconds = poll_seconds
                    if capacity_retries and len(futures) < max_workers:
                        next_retry_at = min(retry_at for _, retry_at, _ in capacity_retries.values())
                        wait_seconds = min(wait_seconds, max(0.0, next_retry_at - self._clock()))
                    if not futures:
                        # Memory/admission blocked every ready task; re-check on the poll interval.
                        time.sleep(wait_seconds)
                        poll_queue(pending, unfinished)
                        continue
                    done, _ = concurrent.futures.wait(
                        futures.keys(),
                        timeout=wait_seconds,
                        return_when=concurrent.futures.FIRST_COMPLETED,
                    )
                    if not done:
                        poll_queue(pending, unfinished)
                        continue
                    collect_completed(done)
                    poll_queue(pending, unfinished)

            self._set_ready_waiting(0)

            if stop_requested:
                # Always consume the stop after in-flight agents drain, even when
                # every started task finished PASSED and nothing remains to start.
                self._record_stop_pause(sprint, stage, unfinished)
                return False

            failed = [
                outcome
                for outcome in outcomes
                if not outcome.ok and not outcome.quota_paused and not outcome.capacity_paused
            ]
            quota_paused = [outcome for outcome in outcomes if outcome.quota_paused]
            capacity_paused = [outcome for _, _, outcome in capacity_retries.values()]
            if failed:
                # Preserve successfully PASSED siblings. Resume will not rerun them.
                self._record_task_failures(sprint, stage, failed)
                print("Blocked tasks: " + ", ".join(f.task_id for f in failed))
                return False
            if quota_paused:
                self._record_quota_pause(sprint, stage, quota_paused)
                for provider in sorted({p.provider or "unknown" for p in quota_paused}):
                    ids = [p.task_id for p in quota_paused if (p.provider or "unknown") == provider]
                    print(f"Waiting (quota, {provider}): " + ", ".join(ids))
                return False
            if capacity_paused:
                self._record_capacity_pause(sprint, stage, capacity_paused)
                groups: dict[tuple[str, str], list[str]] = {}
                for paused in capacity_paused:
                    key = (
                        self._waiting_label(
                            paused.waiting_status, resource_paused=paused.resource_paused
                        ),
                        paused.provider or "unknown",
                    )
                    groups.setdefault(key, []).append(paused.task_id)
                for (cause, provider), ids in sorted(groups.items()):
                    print(f"Waiting ({cause}, {provider}): " + ", ".join(ids))
                return False

            # Merge has begun: remaining queue entries cannot join this stage.
            self._pickup_queue(sprint, current_stage=stage, merge_in_progress=True)

            if not self._integrate_stage(sprint, stage, unfinished, current_commit):
                return False

            # After a successful non-final stage, hand control back so the CLI can
            # resume on a frozen copy of the newly integrated harness code.
            if self.config.self_hosting.enabled and self._open_stages(sprint):
                self._record_restart_on_new_code(sprint, stage)
                return False

        self.finalize_completed_sprint(sprint, dry_run=dry_run)
        return True
