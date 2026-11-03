from __future__ import annotations

import concurrent.futures
import hashlib
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .code_map import CodeMapBuilder
from .config import HarnessConfig
from .environment import EnvironmentManager
from .failure import FailureRecord, FailureStore
from .git_worktree import GitError, WorktreeManager
from .logging_utils import utc_now, write_log
from .models import (
    RESOURCE_LAUNCH_FAILURE_LIMIT,
    SprintSpec,
    TaskSpec,
    TaskStatus,
)
from .agent_runner import AgentResult
from .provider_runner import ProviderAgentRunner
from .state import StateStore
from .release import ReleaseRunner, sprint_is_complete
from .roadmap import Roadmap
from .task_queue import TaskQueueManager
from .verifier import Verifier


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
        self._runtime_root = verifier.log_dir.parent
        self.task_queue = TaskQueueManager(repo_root, self._runtime_root, state)
        self._merge_in_progress: tuple[str, int] | None = None
        self.code_maps = CodeMapBuilder(
            config.code_map, verifier.log_dir.parent, verifier.log_dir, repo_root
        )

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
        runtime.status = TaskStatus.BLOCKED
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
        runtime.status = TaskStatus.BLOCKED
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
        runtime.status = TaskStatus.PENDING
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
        runtime.status = TaskStatus.WAITING_QUOTA
        runtime.waiting_phase = phase
        runtime.waiting_provider = provider
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
        runtime.status = self._waiting_status(cause)
        runtime.waiting_phase = phase
        runtime.waiting_provider = result.provider
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
            runtime.status = TaskStatus.CHECKING
            runtime.waiting_phase = None
            runtime.waiting_provider = None
            self.state.save()
            verification = self.verifier.verify_task(task, worktree, base_ref, env=task_env)
            if not verification.ok:
                runtime.status = TaskStatus.RETRY
                runtime.last_error = verification.summary
                self.state.save()
                return None
            self._record_verification_worktree(task.id, worktree, base_ref)
            verification_summary = verification.summary
            review_required = task.review or self.verifier.requires_review(verification.changed_files)

        if review_required:
            runtime.status = TaskStatus.REVIEWING
            self.state.save()
            review = self.runner.review(
                task, worktree, base_ref, verification_summary, timeout,
                env=task_env, code_map_offered=code_map_offered,
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
                    review = self.runner.review(
                        task, worktree, base_ref, verification_summary, timeout,
                        env=task_env, code_map_offered=code_map_offered,
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
            if not verdict_pass:
                feedback = review.output or review.error or "Reviewer failed without feedback"
                runtime.status = TaskStatus.RETRY
                runtime.review_feedback = feedback
                runtime.last_error = feedback
                self.state.save()
                return None

        try:
            runtime.commit = self.worktrees.commit_all(worktree, task.id)
        except Exception as exc:
            runtime.status = TaskStatus.RETRY
            runtime.last_error = f"Commit failed: {exc}"
            self.state.save()
            return None

        runtime.status = TaskStatus.PASSED
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
                worktree = Path(runtime.worktree)
                # An existing worktree is diffed against the commit it was created from. Using a newer
                # stage base would report every later integration commit as a change by this task.
                if runtime.base_ref:
                    base_ref = runtime.base_ref

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
            runtime.status = TaskStatus.IMPLEMENTING
            runtime.waiting_phase = None
            runtime.waiting_provider = None
            runtime.started_at = runtime.started_at or utc_now()
            runtime.last_error = None
            self.state.save()

            attempt_model_class = (
                model_class_override if getattr(self.runner, "manages_model_routing", False)
                else self._implementation_model_class(task, runtime.attempt, model_class_override=model_class_override)
            )
            result = self.runner.implement(
                task,
                worktree,
                timeout,
                runtime.attempt,
                previous_failure,
                env=task_env,
                model_class_override=attempt_model_class,
                code_map_offered=code_map_offered,
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
                runtime.status = TaskStatus.RETRY
                runtime.last_error = previous_failure
                self.state.save()
                continue

            finished = self._verify_review_commit(
                task, runtime, worktree, base_ref, timeout, task_env, code_map_offered
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
        for task in tasks:
            self.state.get(task.id).status = TaskStatus.MERGING
        self.state.save()
        try:
            return self._integrate_stage_inner(sprint, stage, tasks, checkpoint)
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
                    self.state.get(task.id).status = TaskStatus.PASSED
                self.state.save()
                return False
        except Exception as exc:
            rollback_error = self._rollback_stage(checkpoint)
            error = f"Stage integration failed: {exc}; integration checkout rolled back to the pre-stage checkpoint."
            if rollback_error:
                error += f" ROLLBACK FAILED: {rollback_error}"
            self._record_stage_failure(sprint, stage, tasks, error)
            for task in tasks:
                self.state.get(task.id).status = TaskStatus.PASSED
            self.state.save()
            return False

        finished_at = utc_now()
        for task in tasks:
            runtime = self.state.get(task.id)
            runtime.status = TaskStatus.DONE
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
        ReleaseRunner(self.config, self.repo_root, self.verifier).maybe_run_after_sprint_complete(
            self.state, sprint.id, dry_run=dry_run
        )

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
        return self.task_queue.process_pending(
            sprint,
            self.roadmap,
            current_stage=current_stage,
            stage_tasks=stage_tasks,
            merge_in_progress=merge_in_progress,
            scheduler=self,
        )

    def run_sprint(self, sprint: SprintSpec, *, dry_run: bool = False) -> bool:
        poll_seconds = max(1, self.config.execution.watchdog_poll_seconds)
        dry_run_seen: set[int] = set()
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

                while futures or pending or capacity_retries:
                    # Harvest completions before scheduling retries. A future may have
                    # completed just after the previous FIRST_COMPLETED wait returned;
                    # it must not count as an active sibling that keeps retries in-stage.
                    already_completed = {future for future in futures if future.done()}
                    if already_completed:
                        collect_completed(already_completed)
                        poll_queue(pending, unfinished)

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
                        if len(futures) >= max_workers:
                            break
                        task, _, _ = capacity_retries.pop(task_id)
                        future = pool.submit(self._run_task, task, current_commit)
                        futures[future] = task

                    # Keep the backlog here, so due retries get the first free slot
                    # instead of sitting behind siblings in the executor's FIFO queue.
                    while pending and len(futures) < max_workers:
                        task = pending.pop(0)
                        future = pool.submit(self._run_task, task, current_commit)
                        futures[future] = task

                    wait_seconds = poll_seconds
                    if capacity_retries and len(futures) < max_workers:
                        next_retry_at = min(retry_at for _, retry_at, _ in capacity_retries.values())
                        wait_seconds = min(wait_seconds, max(0.0, next_retry_at - self._clock()))
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

        self.finalize_completed_sprint(sprint, dry_run=dry_run)
        return True
