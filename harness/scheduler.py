from __future__ import annotations

import concurrent.futures
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config import HarnessConfig
from .environment import EnvironmentManager
from .failure import FailureRecord, FailureStore
from .git_worktree import GitError, WorktreeManager
from .logging_utils import utc_now, write_log
from .models import SprintSpec, TaskSpec, TaskStatus
from .provider_runner import ProviderAgentRunner
from .state import StateStore
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
    ):
        self.repo_root = repo_root
        self.config = config
        self.state = state
        self.worktrees = worktrees
        self.runner = runner
        self.verifier = verifier
        self.environment = environment or EnvironmentManager(repo_root, config, verifier.log_dir)
        self.failures = failures or FailureStore(verifier.log_dir.parent / "failures")

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
                    f"python harness.py plan-change propose {task.sprint} --task {task.id}"
                ),
                suggested_command=(
                    f"python harness.py rerun-blocker {task.sprint} --model-class escalation"
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
            suggestion = f"python harness.py rerun-blocker {sprint.id} --model-class escalation"
        else:
            suggestion = (
                f"python harness.py rerun-blocker {sprint.id} --task <TASK_ID> "
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
                suggested_command=f"python harness.py rerun-blocker {sprint.id}",
            )
        )

    def _record_quota_pause(self, sprint: SprintSpec, stage: int, outcomes: list[TaskOutcome]) -> None:
        task_ids = sorted(outcome.task_id for outcome in outcomes)
        self.state.set_meta(f"{sprint.id}.run_status", "WAITING_FOR_QUOTA")
        self.failures.record(
            FailureRecord(
                sprint=sprint.id,
                kind="QUOTA_WAIT",
                stage=stage,
                task_ids=task_ids,
                message=(
                    "Cursor included usage is exhausted. Task state/worktrees were preserved and "
                    "quota pauses did not consume retry budget."
                ),
                suggested_command=f"python harness.py resume {sprint.id}",
            )
        )


    def _record_capacity_pause(self, sprint: SprintSpec, stage: int, outcomes: list[TaskOutcome]) -> None:
        task_ids = sorted(outcome.task_id for outcome in outcomes)
        self.state.set_meta(f"{sprint.id}.run_status", "WAITING_FOR_CAPACITY")
        self.failures.record(
            FailureRecord(
                sprint=sprint.id,
                kind="CAPACITY_WAIT",
                stage=stage,
                task_ids=task_ids,
                message=(
                    "Agent provider capacity/transient infrastructure is temporarily unavailable. Task state/worktrees were preserved and "
                    "capacity pauses did not consume retry budget."
                ),
                suggested_command=f"python harness.py resume {sprint.id}",
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

    def _pause_for_quota(
        self, task: TaskSpec, worktree: Path, *, phase: str, decrement_attempt: bool
    ) -> TaskOutcome:
        runtime = self.state.get(task.id)
        if decrement_attempt and runtime.attempt > 0:
            runtime.attempt -= 1
        runtime.status = TaskStatus.WAITING_FOR_QUOTA
        runtime.waiting_phase = phase
        runtime.last_error = f"Cursor usage quota exhausted during {phase}"
        self.state.save()
        if self.config.quota.teardown_environment_while_waiting:
            self.environment.teardown_task(task, worktree, blocked=True)
        return TaskOutcome(task.id, False, runtime.last_error, quota_paused=True)


    def _pause_for_capacity(
        self, task: TaskSpec, worktree: Path, *, phase: str, decrement_attempt: bool
    ) -> TaskOutcome:
        runtime = self.state.get(task.id)
        if decrement_attempt and runtime.attempt > 0:
            runtime.attempt -= 1
        runtime.status = TaskStatus.WAITING_FOR_CAPACITY
        runtime.waiting_phase = phase
        runtime.last_error = f"Agent provider capacity/transient failure during {phase}"
        self.state.save()
        if self.config.capacity.teardown_environment_while_waiting:
            self.environment.teardown_task(task, worktree, blocked=True)
        return TaskOutcome(task.id, False, runtime.last_error, capacity_paused=True)

    def _verify_review_commit(
        self,
        task: TaskSpec,
        runtime,
        worktree: Path,
        base_ref: str,
        timeout: int,
        task_env: dict[str, str],
    ) -> TaskOutcome | None:
        runtime.status = TaskStatus.VERIFYING
        runtime.waiting_phase = None
        self.state.save()
        verification = self.verifier.verify_task(task, worktree, base_ref, env=task_env)
        if not verification.ok:
            runtime.status = TaskStatus.RETRY
            runtime.last_error = verification.summary
            self.state.save()
            return None

        review_required = task.review or self.verifier.requires_review(verification.changed_files)
        if review_required:
            runtime.status = TaskStatus.REVIEWING
            self.state.save()
            review = self.runner.review(
                task, worktree, base_ref, verification.summary, timeout, env=task_env
            )
            if review.quota_exhausted:
                return self._pause_for_quota(task, worktree, phase="review", decrement_attempt=False)
            if review.capacity_exhausted or review.transient_error:
                return self._pause_for_capacity(task, worktree, phase="review", decrement_attempt=False)
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

        runtime.status = TaskStatus.VERIFIED
        runtime.last_error = None
        runtime.review_feedback = None
        runtime.waiting_phase = None
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

        if runtime.status in {TaskStatus.DONE, TaskStatus.VERIFIED}:
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

        # If quota/capacity was exhausted only at review time, keep the already-written
        # implementation and resume from verification/review without another
        # coding-model call.
        if runtime.status in {TaskStatus.WAITING_FOR_QUOTA, TaskStatus.WAITING_FOR_CAPACITY} and runtime.waiting_phase == "review":
            resumed = self._verify_review_commit(task, runtime, worktree, base_ref, timeout, task_env)
            if resumed is not None:
                return resumed

        previous_failure = runtime.last_error or runtime.review_feedback
        while runtime.attempt < max_attempts and (
            not getattr(self.runner, "manages_model_routing", False)
            or self.runner.has_implementation_budget(task, model_class_override=model_class_override)
        ):
            runtime.attempt += 1
            runtime.status = TaskStatus.RUNNING
            runtime.waiting_phase = None
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
            )
            if result.quota_exhausted:
                return self._pause_for_quota(task, worktree, phase="implement", decrement_attempt=True)
            if result.capacity_exhausted or result.transient_error:
                return self._pause_for_capacity(task, worktree, phase="implement", decrement_attempt=True)
            if not result.ok:
                previous_failure = result.error or "Agent execution failed"
                runtime.status = TaskStatus.RETRY
                runtime.last_error = previous_failure
                self.state.save()
                continue

            finished = self._verify_review_commit(task, runtime, worktree, base_ref, timeout, task_env)
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
        try:
            for task in tasks:
                runtime = self.state.get(task.id)
                if runtime.status != TaskStatus.VERIFIED or not runtime.branch:
                    raise GitError(f"{task.id} is not VERIFIED and cannot be integrated")
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
                return False
        except Exception as exc:
            rollback_error = self._rollback_stage(checkpoint)
            error = f"Stage integration failed: {exc}; integration checkout rolled back to the pre-stage checkpoint."
            if rollback_error:
                error += f" ROLLBACK FAILED: {rollback_error}"
            self._record_stage_failure(sprint, stage, tasks, error)
            return False

        for task in tasks:
            runtime = self.state.get(task.id)
            runtime.status = TaskStatus.DONE
            runtime.finished_at = utc_now()
            self.state.save()
            if not self.config.execution.keep_successful_worktrees and runtime.worktree and runtime.branch:
                self.worktrees.remove(Path(runtime.worktree), runtime.branch, delete_branch=True)
                runtime.worktree = None
                runtime.branch = None
                self.state.save()
        self.failures.clear(sprint.id)
        return True

    def rerun_stage_barrier(self, sprint: SprintSpec, stage: int) -> bool:
        tasks = [task for task in sprint.tasks.values() if task.stage == stage]
        if not tasks:
            raise RuntimeError(f"Sprint {sprint.id} has no stage {stage}")
        not_verified = [
            task.id for task in tasks if self.state.get(task.id).status not in {TaskStatus.VERIFIED, TaskStatus.DONE}
        ]
        if not_verified:
            raise RuntimeError(f"Cannot rerun stage {stage}; tasks not VERIFIED/DONE: {', '.join(not_verified)}")
        pending = [task for task in tasks if self.state.get(task.id).status != TaskStatus.DONE]
        if not pending:
            self.failures.clear(sprint.id)
            return True
        if not self.worktrees.is_clean():
            raise RuntimeError("Integration checkout is dirty; cannot rerun stage barrier safely")
        checkpoint = self.worktrees.head_commit()
        return self._integrate_stage(sprint, stage, pending, checkpoint)

    def run_sprint(self, sprint: SprintSpec, *, dry_run: bool = False) -> bool:
        stages = sorted({task.stage for task in sprint.tasks.values()})
        for stage in stages:
            tasks = [task for task in sprint.tasks.values() if task.stage == stage]
            unfinished = [task for task in tasks if self.state.get(task.id).status != TaskStatus.DONE]
            if not unfinished:
                continue

            self._assert_parallel_safe(unfinished, stage)
            if dry_run:
                print(f"Stage {stage}: " + ", ".join(task.id for task in unfinished))
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

            with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, self.config.execution.max_parallel_agents)
            ) as pool:
                futures = {pool.submit(self._run_task, task, current_commit): task for task in unfinished}
                outcomes = [future.result() for future in concurrent.futures.as_completed(futures)]

            failed = [
                outcome
                for outcome in outcomes
                if not outcome.ok and not outcome.quota_paused and not outcome.capacity_paused
            ]
            quota_paused = [outcome for outcome in outcomes if outcome.quota_paused]
            capacity_paused = [outcome for outcome in outcomes if outcome.capacity_paused]
            if failed:
                # Preserve successfully VERIFIED siblings. Resume will not rerun them.
                self._record_task_failures(sprint, stage, failed)
                print("Blocked tasks: " + ", ".join(f.task_id for f in failed))
                return False
            if quota_paused:
                self._record_quota_pause(sprint, stage, quota_paused)
                print("Waiting for Cursor quota: " + ", ".join(p.task_id for p in quota_paused))
                return False
            if capacity_paused:
                self._record_capacity_pause(sprint, stage, capacity_paused)
                print("Waiting for Cursor provider capacity: " + ", ".join(p.task_id for p in capacity_paused))
                return False

            if not self._integrate_stage(sprint, stage, unfinished, current_commit):
                return False

        self.state.set_meta(f"{sprint.id}.run_status", "COMPLETED")
        self.failures.clear(sprint.id)
        return True
