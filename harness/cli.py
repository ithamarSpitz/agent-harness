from __future__ import annotations

import argparse
import inspect
import json
import subprocess
import sys
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .board_loop import (
    BoardLoop,
    count_running_agents,
    find_active_sprint_id,
    format_parallel_summary,
    memory_estimate_gb,
    should_use_board,
    should_use_console,
    sprint_has_active_run,
    suppress_progress_stdout,
)
from .board import live_headroom_source
from .brief_readthrough import compare_readthrough, parse_readthrough
from .capacity import CapacityManager
from .config import HarnessConfig
from .context import ContextResolver
from .doctor import Doctor, DoctorCheck
from .environment import EnvironmentManager
from .failure import FailureRecord, FailureStore
from .git_worktree import GitError, WorktreeManager
from .manifest import RunManifestManager
from .memory import free_memory_gb
from .models import TaskRuntime, TaskStatus
from .plan_change import PlanChangeManager, _relative_brief_path
from .quota import KeepAwake, QuotaManager, QuotaResetInfo, default_limits_source
from .roadmap import Roadmap, RoadmapError
from .codex_runner import CodexAgentRunner
from .claude_runner import ClaudeAgentRunner
from .agy_runner import AgyAgentRunner
from .copilot_runner import CopilotAgentRunner
from .command_locate import locate
from .provider_runner import ProviderAgentRunner, clear_task_quota_disables
from .process_utils import prepare_external_argv, split_command
from .runner import CursorAgentRunner
from .scheduler import (
    Scheduler,
    apply_release,
    hold_request_path,
    read_hold_request,
    reset_implementation_budget,
    scheduler_is_running,
    update_hold_request,
)
from .state import StateStore
from .usage import UsageRecorder
from .validation import ProjectValidator
from .init_project import run_init
from .limits import run_limits
from .machine_setup import run_setup_machine
from .outcomes_report import import_usage_records, render_models_report
from .release import ReleaseRunner, format_release_status, sprint_is_complete
from .self_host import ensure_frozen_copy, run_resume_with_frozen_copy
from .task_queue import TaskQueueError, TaskQueueManager
from .verifier import Verifier


@dataclass(slots=True)
class Runtime:
    root: Path
    roadmap_path: Path
    config_path: Path
    config: HarnessConfig
    roadmap: Roadmap
    state: StateStore
    worktrees: WorktreeManager
    context: ContextResolver
    environment: EnvironmentManager
    failures: FailureStore
    usage: UsageRecorder
    runner: ProviderAgentRunner
    verifier: Verifier
    scheduler: Scheduler
    manifests: RunManifestManager
    validator: ProjectValidator
    quota: QuotaManager
    capacity: CapacityManager
    plan_changes: PlanChangeManager


def build_runtime(root: Path, roadmap_path: Path, config_path: Path) -> Runtime:
    config = HarnessConfig.load(config_path)
    roadmap = Roadmap(root, roadmap_path)
    runtime_root = root / ".harness"
    state = StateStore(runtime_root / "state.json")
    worktrees = WorktreeManager(root, runtime_root)
    context = ContextResolver(root, config)
    usage = UsageRecorder(runtime_root / "usage.jsonl")
    environment = EnvironmentManager(root, config, runtime_root / "logs")
    failures = FailureStore(runtime_root / "failures")
    cursor_runner = CursorAgentRunner(
        config,
        runtime_root / "logs",
        context,
        usage,
        repo_root=root,
        worktree_root=worktrees.worktree_root,
    )
    codex_runner = CodexAgentRunner(config, runtime_root / "logs", context, usage)
    claude_runner = (
        ClaudeAgentRunner(config, runtime_root / "logs", context, usage)
        if config.claude.enabled else None
    )
    agy_runner = (
        AgyAgentRunner(config, runtime_root / "logs", context, usage)
        if config.agy.enabled else None
    )
    copilot_runner = (
        CopilotAgentRunner(config, runtime_root / "logs", context, usage)
        if config.copilot.enabled else None
    )
    runner = ProviderAgentRunner(
        config,
        state,
        codex_runner,
        cursor_runner,
        usage,
        claude_runner,
        agy_runner,
        copilot=copilot_runner,
    )
    verifier = Verifier(config, runtime_root / "logs", worktrees)
    scheduler = Scheduler(
        root,
        config,
        state,
        worktrees,
        runner,
        verifier,
        environment,
        failures,
        roadmap=roadmap,
        roadmap_path=roadmap_path,
    )
    manifests = RunManifestManager(root, runtime_root, state)
    validator = ProjectValidator(root, roadmap, config)
    def routing_limits(now):
        found = list(default_limits_source(now))
        for key, value in state.meta().items():
            if not key.endswith(".routing_quota_reset_at"):
                continue
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            reset = datetime.fromtimestamp(float(value), tz=timezone.utc)
            if reset > now:
                found.append(QuotaResetInfo("claude", reset))
        return found

    quota = QuotaManager(config, state, limits_source=routing_limits)
    capacity = CapacityManager(config, state)
    plan_changes = PlanChangeManager(root, runtime_root, state, failures)
    return Runtime(
        root=root,
        roadmap_path=roadmap_path,
        config_path=config_path,
        config=config,
        roadmap=roadmap,
        state=state,
        worktrees=worktrees,
        context=context,
        environment=environment,
        failures=failures,
        usage=usage,
        runner=runner,
        verifier=verifier,
        scheduler=scheduler,
        manifests=manifests,
        validator=validator,
        quota=quota,
        capacity=capacity,
        plan_changes=plan_changes,
    )


def cmd_status(
    roadmap: Roadmap,
    state: StateStore,
    sprint_id: str | None,
    runtime_root: Path | None = None,
    *,
    config: HarnessConfig | None = None,
) -> int:
    queue = TaskQueueManager(roadmap.root, runtime_root or (roadmap.root / ".harness"), state)
    sprint_ids = [sprint_id] if sprint_id else list(roadmap.sprints)
    held_ids = set(read_hold_request(hold_request_path(runtime_root or (roadmap.root / ".harness"))))
    for sid in sprint_ids:
        sprint = roadmap.sprint(sid)
        queue.merge_added_tasks_from_state(sprint)
        print(f"{sid}: {sprint.name}")
        release_line = format_release_status(state, sid)
        if release_line:
            print(f"  {release_line}")
        if config is not None and sprint_has_active_run(state, sid):
            running = count_running_agents(state, sprint)
            maximum = max(1, int(config.execution.max_parallel_agents))
            print(
                "  "
                + format_parallel_summary(
                    running=running,
                    maximum=maximum,
                    free_gb=free_memory_gb(),
                    per_agent_gb=memory_estimate_gb(state, config),
                )
            )
        for stage in sorted({t.stage for t in sprint.tasks.values()}):
            print(f"  Stage {stage}")
            for task in sorted((t for t in sprint.tasks.values() if t.stage == stage), key=lambda x: x.id):
                rt = state.get(task.id)
                label = "held" if task.id in held_ids else rt.status.value
                suffix = f" attempt={rt.attempt}" if rt.attempt else ""
                if rt.added:
                    suffix += " (added)"
                if rt.last_error:
                    suffix += f" error={rt.last_error.splitlines()[0][:100]}"
                print(f"    {label:10} {task.id}{suffix}")
        for task_id, reason in queue.rejected_entries(sid):
            short = reason.splitlines()[0][:100]
            print(f"    REJECTED   {task_id} {short}")
    return 0


def cmd_watch(runtime: Runtime, sprint_id: str | None) -> int:
    with KeepAwake(runtime.config.execution.keep_awake_during_run):
        return _cmd_watch_live(runtime, sprint_id)


def _cmd_watch_live(runtime: Runtime, sprint_id: str | None) -> int:
    resolved = sprint_id or find_active_sprint_id(runtime.roadmap, runtime.state)
    if resolved is None:
        print("No active sprint run to watch; pass a sprint id.", file=sys.stderr)
        return 2
    # Resolve once so a missing sprint fails before the loop.
    runtime.roadmap.sprint(resolved)
    interactive = should_use_console(sys.stdout, sys.stdin)
    loop = BoardLoop(
        runtime,
        resolved,
        stream=sys.stdout,
        reload_from_disk=True,
        stop_when_finished=True,
        interactive=interactive,
        mode="watch",
        headroom_source=lambda: live_headroom_source(runtime.config),
    )
    try:
        loop.run_until_stopped()
    except KeyboardInterrupt:
        loop.stop()
    finally:
        loop.close()
    return 0


def cmd_add_task(
    runtime: Runtime,
    sprint_id: str,
    brief_path: Path,
    *,
    stage_choice: str,
) -> int:
    if stage_choice not in {"auto", "current", "next"}:
        print(f"Invalid --stage value {stage_choice!r}; expected auto, current, or next", file=sys.stderr)
        return 2
    try:
        queued = runtime.scheduler.task_queue.enqueue(
            brief_path,
            sprint_id,
            runtime.roadmap,
            stage_choice=stage_choice,
        )
    except TaskQueueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(f"Queued task at {queued}")
    return 0


def cmd_release(runtime: Runtime, sprint_id: str) -> int:
    sprint = runtime.roadmap.sprint(sprint_id)
    runtime.scheduler.task_queue.merge_added_tasks_from_state(sprint)
    if not sprint_is_complete(runtime.state, sprint):
        print(
            f"Sprint {sprint_id} is not complete; release runs only after every task is DONE.",
            file=sys.stderr,
        )
        return 2
    if not runtime.config.release.commands:
        print(f"Sprint {sprint_id} has no release.commands configured; nothing to run.")
        return 0
    result = ReleaseRunner(runtime.config, runtime.root, runtime.verifier).run_and_persist(
        runtime.state, sprint_id
    )
    if not result.ok:
        print(
            f"Release failed: {result.failed_command} (exit {result.exit_code}). "
            f"Merged sprint work is unchanged; retry with: harness release {sprint_id}",
            file=sys.stderr,
        )
        return 1
    print(f"Release completed for sprint {sprint_id}.")
    return 0


def cmd_graph(roadmap: Roadmap, sprint_id: str) -> int:
    sprint = roadmap.sprint(sprint_id)
    print(f"{sprint.id}: {sprint.name}")
    for stage in sorted({t.stage for t in sprint.tasks.values()}):
        print(f"Stage {stage}:")
        for task in sorted((t for t in sprint.tasks.values() if t.stage == stage), key=lambda x: x.id):
            deps = ", ".join(task.depends_on) if task.depends_on else "(root)"
            print(f"  {task.id} <- {deps}")
    return 0


def cmd_validate(runtime: Runtime, sprint_id: str | None = None) -> int:
    report = runtime.validator.validate(sprint_id)
    print(report.render())
    return 0 if report.ok else 2


def cmd_brief_check(runtime: Runtime, task_id: str, timeout_minutes: int) -> int:
    task = _resolve_task(runtime, task_id)
    brief_text = task.file.read_text(encoding="utf-8-sig")
    result = runtime.runner.brief_check(
        task, runtime.root, brief_text, timeout_minutes
    )
    if not result.ok:
        if result.error:
            reason = result.error
        elif result.provider == "codex":
            reason = (
                "Codex quota exhausted"
                if result.quota_exhausted else "Codex brief check failed"
            )
        else:
            reason = (
                "Provider quota exhausted"
                if result.quota_exhausted else "Provider brief check failed"
            )
        print(reason, file=sys.stderr)
        return 2

    readthrough = parse_readthrough(result.output)
    messages = compare_readthrough(readthrough, task.allowed_paths)
    print("Files:")
    if readthrough.files:
        for path in readthrough.files:
            print(f"  {path}")
    else:
        print("  (none)")
    print("Plans:")
    if readthrough.plans:
        for path, plan in readthrough.plans.items():
            print(f"  {path}: {plan}")
    else:
        print("  (none)")
    if messages:
        for message in messages:
            print(message)
        return 1
    print("OK brief reads as intended")
    return 0


def cmd_doctor(runtime: Runtime, *, full: bool) -> int:
    checks = Doctor(runtime.root, runtime.config, runtime.worktrees).run(full=full)
    if runtime.config.claude.enabled:
        parts = split_command(runtime.config.claude.command)
        try:
            proc = subprocess.run(
                prepare_external_argv([*parts, "--version"]),
                cwd=runtime.root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=15,
            )
            detail = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
            checks.append(DoctorCheck(
                "claude-cli/login", proc.returncode == 0,
                detail[:300] or "claude --version returned no output",
                required=not (
                    runtime.config.agy.enabled
                    or runtime.config.codex.enabled
                    or runtime.config.cursor.enabled
                ),
            ))
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            checks.append(DoctorCheck(
                "claude-cli/login", False, str(exc),
                required=not (
                    runtime.config.agy.enabled
                    or runtime.config.codex.enabled
                    or runtime.config.cursor.enabled
                ),
            ))
    if runtime.config.agy.enabled:
        configured_command = runtime.config.agy.command
        executable = (
            configured_command
            if Path(configured_command).is_absolute()
            else locate(configured_command)
        )
        missing_message = (
            "agy not found on PATH (installed after this shell started? restart the shell "
            "or set agy.command to the full path)"
        )
        try:
            if executable is None:
                raise FileNotFoundError(missing_message)
            proc = subprocess.run(
                prepare_external_argv([executable, "--version"]),
                cwd=runtime.root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=15,
            )
            detail = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
            checks.append(DoctorCheck(
                "agy-cli/login", proc.returncode == 0,
                detail[:300] or "agy --version returned no output",
                required=not (
                    runtime.config.codex.enabled
                    or runtime.config.cursor.enabled
                    or runtime.config.claude.enabled
                ),
            ))
        except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
            checks.append(DoctorCheck(
                "agy-cli/login", False, str(exc),
                required=not (
                    runtime.config.codex.enabled
                    or runtime.config.cursor.enabled
                    or runtime.config.claude.enabled
                ),
            ))
    print(Doctor.render(checks))
    if full:
        report = runtime.validator.validate()
        print(report.render())
        if not report.ok:
            return 1
    return 0 if all(check.ok or not check.required for check in checks) else 1


def _prepare_sprint_base(
    config: HarnessConfig,
    state: StateStore,
    worktrees: WorktreeManager,
    sprint_id: str,
) -> None:
    """Pin the sprint to one integration branch and validate its initial base_ref."""
    current_branch = worktrees.current_branch()
    branch_key = f"{sprint_id}.integration_branch"
    recorded_branch = state.get_meta(branch_key)
    if recorded_branch is None:
        state.set_meta(branch_key, current_branch)
    elif recorded_branch != current_branch:
        raise RuntimeError(
            f"Sprint {sprint_id} is pinned to integration branch {recorded_branch}, "
            f"but the current branch is {current_branch}"
        )

    initial_key = f"{sprint_id}.initial_base_commit"
    configured_key = f"{sprint_id}.configured_base_ref"
    initial_commit = state.get_meta(initial_key)
    configured_ref = config.execution.base_ref
    stored_configured_ref = state.get_meta(configured_key)

    if initial_commit is None:
        current_head = worktrees.head_commit()
        if configured_ref:
            expected = worktrees.resolve_ref(configured_ref)
            if current_head != expected:
                raise RuntimeError(
                    f"Configured execution.base_ref={configured_ref!r} resolves to {expected}, "
                    f"but the current integration checkout is at {current_head}. "
                    "Check out/reset the intended base before starting this sprint."
                )
        state.set_meta(initial_key, current_head)
        state.set_meta(configured_key, configured_ref)
        return

    if stored_configured_ref != configured_ref:
        raise RuntimeError(
            f"execution.base_ref changed while sprint {sprint_id} is in progress: "
            f"was {stored_configured_ref!r}, now {configured_ref!r}"
        )




def _run_pending_revalidations(runtime: Runtime, current_sprint: str) -> bool:
    pending_value = runtime.state.get_meta("plan.pending_revalidation", [])
    if not isinstance(pending_value, list):
        raise RuntimeError("Invalid plan.pending_revalidation state; expected a list")
    pending = [str(x) for x in pending_value]
    if not pending:
        return True
    remaining = list(pending)
    for task_id in pending:
        try:
            task = runtime.roadmap.task(task_id)
        except RoadmapError:
            runtime.failures.record(
                FailureRecord(
                    sprint=current_sprint,
                    kind="REVALIDATION_FAILED",
                    task_ids=[task_id],
                    message=f"Pending revalidation task {task_id} no longer exists in the revised roadmap.",
                    suggested_command="Repair the plan revision before continuing.",
                )
            )
            return False
        task_env = runtime.environment.task_env(task, runtime.root)
        env_result = runtime.environment.prepare_task(task, runtime.root)
        if not env_result.ok:
            result = None
            failure_summary = env_result.message
        else:
            result = runtime.verifier.verify_integrated_task(task, runtime.root, env=task_env)
            runtime.environment.teardown_task(task, runtime.root, blocked=not result.ok)
            failure_summary = result.summary
        if result is None or not result.ok:
            runtime.failures.record(
                FailureRecord(
                    sprint=current_sprint,
                    kind="REVALIDATION_FAILED",
                    stage=task.stage,
                    task_ids=[task.id],
                    message=f"Previously integrated task {task.id} failed post-plan-change revalidation.\n{failure_summary}",
                    suggested_command=(
                        f"Analyze a corrective plan change: harness plan-change propose "
                        f"{task.sprint} --task {task.id}"
                    ),
                )
            )
            runtime.state.set_meta("plan.pending_revalidation", remaining)
            return False
        remaining.remove(task_id)
        runtime.state.set_meta("plan.pending_revalidation", remaining)
    return True


def _check_plan_gate(runtime: Runtime) -> None:
    open_request = runtime.plan_changes.open_request_id()
    if open_request:
        raise RuntimeError(
            f"Plan change {open_request} is awaiting human apply/reject. "
            f"Inspect it with `harness plan-change status {open_request}`."
        )

def cmd_run(
    runtime: Runtime,
    sprint_id: str,
    dry_run: bool,
    *,
    no_board: bool = False,
) -> int:
    report = runtime.validator.validate(sprint_id)
    if not report.ok:
        print(report.render(), file=sys.stderr)
        return 2

    runtime.worktrees.ensure_repo()
    runtime.plan_changes.ensure_baseline(runtime.roadmap)
    if not dry_run:
        try:
            _check_plan_gate(runtime)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 3

    if not dry_run and not runtime.worktrees.is_clean():
        message = (
            "Refusing to run: the integration checkout is dirty. Atomic stage rollback requires a clean "
            "checkpoint; commit or stash changes first."
        )
        runtime.failures.record(
            FailureRecord(
                sprint=sprint_id,
                kind="RUN_PRECONDITION",
                message=message,
                suggested_command=f"Fix the checkout, then: harness resume {sprint_id}",
            )
        )
        print(message, file=sys.stderr)
        return 2
    sprint = runtime.roadmap.sprint(sprint_id)
    if dry_run:
        queue = getattr(runtime.scheduler, "task_queue", None)
        if queue is not None:
            queue.merge_added_tasks_from_state(sprint)
        ok = runtime.scheduler.run_sprint(sprint, dry_run=True)
        return 0 if ok else 3

    with KeepAwake(runtime.config.execution.keep_awake_during_run):
        # Tests may stub `_cmd_run_live` without the newer keyword.
        if "no_board" in inspect.signature(_cmd_run_live).parameters:
            return _cmd_run_live(runtime, sprint_id, sprint, no_board=no_board)
        return _cmd_run_live(runtime, sprint_id, sprint)


def _cmd_run_live(
    runtime: Runtime,
    sprint_id: str,
    sprint,
    *,
    no_board: bool = False,
) -> int:
    runtime.scheduler.task_queue.merge_added_tasks_from_state(sprint)
    if not _run_pending_revalidations(runtime, sprint_id):
        record = runtime.failures.get(sprint_id)
        if record:
            print(runtime.failures.render(record), file=sys.stderr)
        return 3

    if all(runtime.state.get(task.id).status == TaskStatus.DONE for task in sprint.tasks.values()):
        # Last-stage `rerun-blocker` marks tasks DONE without going through `run_sprint`.
        # Drain the queue first: a brief queued during the barrier (or while the
        # last stage was merging) must still be placed in a new stage on resume.
        stages = sorted({task.stage for task in sprint.tasks.values()}) or [1]
        runtime.scheduler._pickup_queue(
            sprint, current_stage=stages[-1], merge_in_progress=True
        )

    if all(runtime.state.get(task.id).status == TaskStatus.DONE for task in sprint.tasks.values()):
        # Finalize (and run release once) so `resume` still performs the sprint's last step.
        runtime.scheduler.finalize_completed_sprint(sprint)
        print(f"Sprint {sprint_id} is already complete; nothing to resume/run.")
        return 0

    board: BoardLoop | None = None
    use_board = should_use_board(sys.stdout, no_board=no_board)
    try:
        _prepare_sprint_base(runtime.config, runtime.state, runtime.worktrees, sprint_id)
        manifest_path = runtime.manifests.start_or_resume(
            sprint=sprint,
            roadmap_path=runtime.roadmap_path,
            config_path=runtime.config_path,
            config=runtime.config,
            integration_branch=runtime.worktrees.current_branch(),
            started_from_commit=str(runtime.state.get_meta(f"{sprint_id}.initial_base_commit")),
        )
        print(f"Run manifest: {manifest_path.relative_to(runtime.root)}")

        real_stdout = sys.stdout
        if use_board:
            board = BoardLoop(
                runtime,
                sprint_id,
                stream=real_stdout,
                reload_from_disk=False,
                stop_when_finished=False,
                interactive=should_use_console(real_stdout, sys.stdin, no_board=no_board),
                mode="run",
                headroom_source=lambda: live_headroom_source(runtime.config),
            )
            board.start_background()

        def _run_loop() -> int:
            while True:
                ok = runtime.scheduler.run_sprint(sprint, dry_run=False)
                if ok:
                    runtime.manifests.finish(sprint_id, success=True)
                    return 0

                record = runtime.failures.get(sprint_id)
                if record is not None and record.get("kind") == "QUOTA_WAIT":
                    runtime.manifests.set_status(sprint_id, "WAITING_QUOTA")
                    print(runtime.failures.render(record), file=sys.stderr)
                    if runtime.config.quota.policy == "wait":
                        seconds = runtime.quota.next_wait_seconds()
                        print(
                            f"Quota policy=wait: preserving state and sleeping about {seconds / 60:.1f} minutes "
                            "before retrying. Ctrl+C is safe; `resume` will continue later.",
                            file=sys.stderr,
                        )
                        # The outer run-level KeepAwake remains active. QuotaManager's
                        # own context is intentionally harmless/nested and also keeps
                        # standalone wait calls safe.
                        runtime.quota.wait_once(sprint_id=sprint_id)
                        runtime.manifests.set_status(sprint_id, "IMPLEMENTING")
                        continue
                    return 4

                if record is not None and record.get("kind") == "CAPACITY_WAIT":
                    waiting = str(runtime.state.get_meta(f"{sprint_id}.run_status") or "WAITING_PROVIDER_ERROR")
                    runtime.manifests.set_status(sprint_id, waiting)
                    print(runtime.failures.render(record), file=sys.stderr)
                    if runtime.config.capacity.policy == "wait":
                        seconds = runtime.capacity.next_wait_seconds()
                        print(
                            f"Capacity policy=wait: preserving state and retrying in about "
                            f"{seconds / 60:.1f} minutes. Ctrl+C is safe; `resume` will continue later.",
                            file=sys.stderr,
                        )
                        runtime.capacity.wait_once(sprint_id=sprint_id)
                        runtime.manifests.set_status(sprint_id, "IMPLEMENTING")
                        continue
                    return 5

                if record is not None and record.get("kind") == "RESTART_ON_NEW_CODE":
                    commit = runtime.worktrees.head_commit()
                    target = ensure_frozen_copy(runtime.root, commit)
                    if target is None:
                        print(
                            "WARNING: self-host frozen copy failed; continuing on current code.",
                            file=sys.stderr,
                        )
                        runtime.failures.clear(sprint_id)
                        runtime.manifests.set_status(sprint_id, "IMPLEMENTING")
                        continue
                    try:
                        roadmap_arg = str(runtime.roadmap_path.relative_to(runtime.root))
                    except ValueError:
                        roadmap_arg = str(runtime.roadmap_path)
                    try:
                        config_arg = str(runtime.config_path.relative_to(runtime.root))
                    except ValueError:
                        config_arg = str(runtime.config_path)
                    # KeepAwake remains active in the outer cmd_run context while we wait.
                    return run_resume_with_frozen_copy(
                        sprint_id,
                        target,
                        repo_root=runtime.root,
                        roadmap=roadmap_arg,
                        config=config_arg,
                    )

                runtime.manifests.finish(sprint_id, success=False)
                if record is not None:
                    print(runtime.failures.render(record), file=sys.stderr)
                return 3

        run_ownership = getattr(runtime.scheduler, "run_ownership", None)
        ownership = run_ownership(sprint_id) if callable(run_ownership) else nullcontext()
        with ownership:
            if use_board:
                with suppress_progress_stdout(real_stdout):
                    return _run_loop()
            return _run_loop()
    except KeyboardInterrupt:
        runtime.manifests.set_status(sprint_id, "PAUSED_BY_USER")
        print("Run interrupted safely; state is persisted. Use `resume` to continue.", file=sys.stderr)
        return 130
    except Exception as exc:
        message = str(exc)
        runtime.failures.record(
            FailureRecord(
                sprint=sprint_id,
                kind="RUN_INFRASTRUCTURE",
                message=message,
                suggested_command=f"Fix the environment/precondition, then: harness resume {sprint_id}",
            )
        )
        try:
            runtime.manifests.finish(sprint_id, success=False)
        except Exception:
            pass
        print(f"Run stopped: {message}", file=sys.stderr)
        return 3
    finally:
        if board is not None:
            board.close()


def _reset_implementation_budget(task_runtime: TaskRuntime) -> None:
    """Clear attempt and per-provider counters so the next run has a real implement budget."""
    reset_implementation_budget(task_runtime)


def _held_task_for_command(runtime: Runtime, task_id: str):
    """Resolve a task for hold/release; print a message and return None when unusable."""
    try:
        task = _resolve_task(runtime, task_id)
    except RoadmapError:
        print(f"Unknown task {task_id}; nothing changed.")
        return None
    active = find_active_sprint_id(runtime.roadmap, runtime.state)
    if active is not None and task.sprint != active:
        print(f"{task_id} is not in the current sprint ({active}); nothing changed.")
        return None
    return task


def cmd_hold(runtime: Runtime, task_id: str) -> int:
    task = _held_task_for_command(runtime, task_id)
    if task is None:
        return 2
    path = hold_request_path(runtime.root / ".harness")
    if task_id in read_hold_request(path):
        print(f"{task_id} is already held; nothing changed.")
        return 0
    current = runtime.state.get(task_id).status
    if current in {TaskStatus.DONE, TaskStatus.PASSED, TaskStatus.MERGING}:
        print(f"{task_id} is {current.value}; nothing changed.")
        return 2
    update_hold_request(path, add=task_id)
    print(f"{task_id} held: it starts no new attempt or review; release with `harness release {task_id}`.")
    return 0


def cmd_release_task(runtime: Runtime, task_id: str, *, fresh_budget: bool = False) -> int:
    task = _held_task_for_command(runtime, task_id)
    if task is None:
        return 2
    path = hold_request_path(runtime.root / ".harness")
    if task_id not in read_hold_request(path):
        print(f"{task_id} is not held; nothing changed.")
        return 2
    if scheduler_is_running(runtime.root / ".harness", task.sprint):
        # A scheduler owns state.json: only leave a `released` entry, which it
        # applies (RETRY and the fresh budget) itself.  Saving this process's
        # snapshot would overwrite the scheduler's progress on other tasks.
        update_hold_request(path, remove=task_id, release_fresh_budget=fresh_budget)
    else:
        # No live scheduler owns state.json: apply the release directly.  A
        # persisted active run id is not proof of liveness after a stop/failure.
        update_hold_request(path, remove=task_id)
        apply_release(runtime.state, task_id, fresh_budget=fresh_budget)
    print(f"{task_id} released" + (" with a fresh budget" if fresh_budget else "") + ".")
    return 0


def cmd_retry(state: StateStore, task_id: str) -> int:
    runtime = state.get(task_id)
    runtime.status = TaskStatus.RETRY
    runtime.last_error = "Manual retry requested"
    runtime.review_feedback = None
    _reset_implementation_budget(runtime)
    clear_task_quota_disables(state, task_id)
    state.save()
    print(f"{task_id} reset for a fresh retry budget (attempt, provider_attempts, stall_counts)")
    return 0


def cmd_unblock(state: StateStore, task_id: str) -> int:
    runtime = state.get(task_id)
    if runtime.status != TaskStatus.BLOCKED:
        print(f"{task_id} is {runtime.status.value}, not BLOCKED", file=sys.stderr)
        return 2
    runtime.status = TaskStatus.RETRY
    runtime.last_error = "Manually unblocked"
    runtime.review_feedback = None
    _reset_implementation_budget(runtime)
    clear_task_quota_disables(state, task_id)
    state.save()
    print(f"{task_id} unblocked with a fresh retry budget (attempt, provider_attempts, stall_counts)")
    return 0


def _resolve_task(runtime: Runtime, task_id: str):
    return runtime.scheduler.task_queue.resolve_task(runtime.roadmap, task_id)


def cmd_reset_task(runtime: Runtime, task_id: str) -> int:
    task = _resolve_task(runtime, task_id)
    task_runtime = runtime.state.get(task_id)
    if task_runtime.status == TaskStatus.DONE:
        print(
            f"Refusing to reset DONE task {task_id}: its commit is already integrated. "
            "Reset the sprint/integration history manually if you truly want to undo it.",
            file=sys.stderr,
        )
        return 2
    cleaned = False
    if task_runtime.worktree and task_runtime.branch:
        worktree = Path(task_runtime.worktree)
        try:
            runtime.worktrees.remove(worktree, task_runtime.branch, delete_branch=True)
        except GitError:
            print(
                f"could not remove {worktree}: close programs using it and run reset-task again",
                file=sys.stderr,
            )
            return 2
        if worktree.exists():
            print(
                f"could not remove {worktree}: close programs using it and run reset-task again",
                file=sys.stderr,
            )
            return 2
        cleaned = True
    elif task_runtime.branch:
        runtime.worktrees.delete_branch(task_runtime.branch)
        cleaned = True
    clear_task_quota_disables(runtime.state, task_id)
    runtime.state.reset_task(task_id)
    runtime.failures.resolve_task(task.sprint, task_id)
    if cleaned:
        print(f"{task_id} reset to PENDING; task-owned worktree/branch cleaned")
    else:
        print(f"{task_id} reset to PENDING; task-owned worktree/branch cleaned when present")
    return 0


def cmd_verify(runtime: Runtime, task_id: str) -> int:
    task = _resolve_task(runtime, task_id)
    task_runtime = runtime.state.get(task_id)
    if not task_runtime.worktree or not task_runtime.base_ref:
        print(f"{task_id} has no active worktree/base_ref", file=sys.stderr)
        return 2
    result = runtime.verifier.verify_task(task, Path(task_runtime.worktree), task_runtime.base_ref)
    print(result.summary)
    return 0 if result.ok else 3


def cmd_review(runtime: Runtime, task_id: str, timeout: int) -> int:
    task = _resolve_task(runtime, task_id)
    task_runtime = runtime.state.get(task_id)
    if not task_runtime.worktree or not task_runtime.base_ref:
        print(f"{task_id} has no active worktree/base_ref", file=sys.stderr)
        return 2
    worktree = Path(task_runtime.worktree)
    task_env = runtime.environment.task_env(task, worktree)
    runtime.scheduler.code_maps.ensure_project_map(task_runtime.base_ref, env=task_env)
    code_map_offered = runtime.scheduler.code_maps.offer_to_worktree(task, worktree)
    result = runtime.runner.review(
        task, worktree, task_runtime.base_ref, "Manual review requested", timeout,
        env=task_env, code_map_offered=code_map_offered,
    )
    print(result.output)
    return 0 if result.ok and "VERDICT: PASS" in result.output else 3


def cmd_run_task(
    runtime: Runtime,
    task_id: str,
    *,
    model_class: str | None = None,
    fresh_budget: bool = False,
) -> int:
    task = _resolve_task(runtime, task_id)
    report = runtime.validator.validate(task.sprint)
    if not report.ok:
        print(report.render(), file=sys.stderr)
        return 2
    if model_class is not None and model_class not in runtime.config.cursor.models:
        print(f"Unknown model class {model_class!r}; available: {', '.join(runtime.config.cursor.models)}", file=sys.stderr)
        return 2
    missing = [dep for dep in task.depends_on if runtime.state.get(dep).status != TaskStatus.DONE]
    if missing:
        print(f"Cannot run {task_id}; dependencies not DONE: {', '.join(missing)}", file=sys.stderr)
        return 2

    task_runtime = runtime.state.get(task_id)
    if fresh_budget:
        if task_runtime.status == TaskStatus.DONE:
            print(f"Refusing focused rerun of DONE task {task_id}; it is already integrated.", file=sys.stderr)
            return 2
        task_runtime.status = TaskStatus.RETRY
        task_runtime.last_error = task_runtime.last_error or "Focused rerun requested"
        task_runtime.review_feedback = None
        task_runtime.commit = None
        _reset_implementation_budget(task_runtime)
        clear_task_quota_disables(runtime.state, task_id)
        runtime.state.save()

    base_commit = task_runtime.base_ref or subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=runtime.worktrees.repo_root,
        text=True, encoding="utf-8", errors="replace",
        capture_output=True,
        check=True,
    ).stdout.strip()
    with KeepAwake(runtime.config.execution.keep_awake_during_run):
        outcome = runtime.scheduler._run_task(
            task, base_commit, model_class_override=model_class
        )  # intentional focused/debugging entrypoint
    if outcome.stopped:
        print(
            f"{task_id} was not run: a stop request is pending; state preserved "
            f"(run `harness resume {task.sprint}` to continue).",
            file=sys.stderr,
        )
        return 3
    if outcome.ok:
        runtime.failures.resolve_task(task.sprint, task_id)
        print(f"{task_id} is PASSED in its worktree; run `harness resume {task.sprint}` to continue from this stage.")
        return 0
    if outcome.quota_paused:
        runtime.failures.record(
            FailureRecord(
                sprint=task.sprint,
                kind="QUOTA_WAIT",
                stage=task.stage,
                task_ids=[task.id],
                message=outcome.error or "Provider usage quota exhausted",
                suggested_command=f"harness resume {task.sprint}",
            )
        )
        print(outcome.error or "Provider usage quota exhausted; state preserved", file=sys.stderr)
        return 4
    if outcome.capacity_paused:
        runtime.failures.record(
            FailureRecord(
                sprint=task.sprint,
                kind="CAPACITY_WAIT",
                stage=task.stage,
                task_ids=[task.id],
                message=outcome.error or "Provider call is temporarily unavailable",
                suggested_command=f"harness resume {task.sprint}",
            )
        )
        print(outcome.error or "Provider call is temporarily unavailable; state preserved", file=sys.stderr)
        return 5
    print(outcome.error or "task failed", file=sys.stderr)
    return 3


def cmd_failure(runtime: Runtime, sprint_id: str) -> int:
    record = runtime.failures.get(sprint_id)
    if record is None:
        print(f"No active failure recorded for {sprint_id}.")
        return 0
    print(runtime.failures.render(record))
    return 0


def cmd_rerun_blocker(
    runtime: Runtime, sprint_id: str, model_class: str | None, task_id: str | None = None
) -> int:
    record = runtime.failures.get(sprint_id)
    if record is None:
        print(f"No active failure recorded for {sprint_id}; use `resume` if work remains.")
        return 0
    kind = str(record.get("kind"))
    if kind == "TASK_BLOCKED":
        task_ids_value = record.get("task_ids")
        if not isinstance(task_ids_value, list):
            print("Task-blocked failure record has invalid task_ids.", file=sys.stderr)
            return 2
        task_ids = [str(x) for x in task_ids_value]
        if task_id is not None:
            if task_id not in task_ids:
                print(
                    f"{task_id} is not one of the current blocked tasks: {', '.join(task_ids)}",
                    file=sys.stderr,
                )
                return 2
            selected = task_id
        elif len(task_ids) == 1:
            selected = task_ids[0]
        else:
            print(
                "Multiple tasks are blocked: " + ", ".join(task_ids)
                + ". Re-run one with --task <TASK_ID>.",
                file=sys.stderr,
            )
            return 2
        return cmd_run_task(runtime, selected, model_class=model_class, fresh_budget=True)
    if kind == "STAGE_BARRIER":
        stage = record.get("stage")
        if not isinstance(stage, int) or isinstance(stage, bool):
            print("Stage failure record has no valid stage number.", file=sys.stderr)
            return 2
        sprint = runtime.roadmap.sprint(sprint_id)
        runtime.scheduler.task_queue.merge_added_tasks_from_state(sprint)
        ok = runtime.scheduler.rerun_stage_barrier(sprint, stage)
        if ok:
            print(f"Stage {stage} barrier passed. Run `harness resume {sprint_id}` to continue with the next stage.")
            return 0
        refreshed = runtime.failures.get(sprint_id)
        if refreshed:
            print(runtime.failures.render(refreshed), file=sys.stderr)
        return 3
    print(f"Unsupported failure kind: {kind}", file=sys.stderr)
    return 2


def cmd_inspect(runtime: Runtime, task_id: str) -> int:
    task = _resolve_task(runtime, task_id)
    task_runtime = runtime.state.get(task_id)
    payload: dict[str, object] = {
        "task": {
            "id": task.id,
            "sprint": task.sprint,
            "stage": task.stage,
            "file": _relative_brief_path(task.file, runtime.root),
            "depends_on": task.depends_on,
            "model_class": task.model_class,
            "allowed_paths": task.allowed_paths,
            "verification": task.verification,
            "context": task.context_refs,
            "environment": task.environment,
        },
        "runtime": task_runtime.to_json(),
        "resolved_context": runtime.context.display_paths(task),
    }
    if task_runtime.worktree and task_runtime.base_ref and Path(task_runtime.worktree).exists():
        payload["changed_files"] = runtime.worktrees.changed_files(Path(task_runtime.worktree), task_runtime.base_ref)
    logs = sorted((runtime.root / ".harness" / "logs").glob(f"{task_id}-*"))
    payload["logs"] = [path.relative_to(runtime.root).as_posix() for path in logs]
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def cmd_cleanup(runtime: Runtime, *, stale_worktrees: bool, completed: bool) -> int:
    did_anything = False
    if stale_worktrees:
        removed = runtime.worktrees.cleanup_stale_worktree_dirs()
        did_anything = True
        print(f"Pruned stale worktree metadata/directories: {len(removed)}")
        for path in removed:
            print(f"  {path}")
    if completed:
        count = 0
        for task_id, task_runtime in runtime.state.all().items():
            if task_runtime.status != TaskStatus.DONE or not task_runtime.worktree or not task_runtime.branch:
                continue
            worktree = Path(task_runtime.worktree)
            try:
                runtime.worktrees.remove(worktree, task_runtime.branch, delete_branch=True)
            except GitError as exc:
                print(f"WARNING: {exc}", file=sys.stderr)
                continue
            task_runtime.worktree = None
            task_runtime.branch = None
            runtime.state.save()
            count += 1
        did_anything = True
        print(f"Removed retained worktrees for DONE tasks: {count}")
    if not did_anything:
        print("Nothing selected. Use --stale-worktrees and/or --completed.", file=sys.stderr)
        return 2
    return 0


def cmd_manifest(runtime: Runtime, sprint_id: str) -> int:
    path = runtime.manifests.current_path(sprint_id)
    if path is None:
        print(f"No active run manifest for {sprint_id}", file=sys.stderr)
        return 2
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def cmd_usage(runtime: Runtime) -> int:
    summary = runtime.usage.summary_by_provider()
    if not summary:
        print("No agent usage records yet.")
        return 0
    print("provider model                         calls failures quota network busy server timeout switches minutes")
    for key, row in sorted(summary.items()):
        provider, _, model = key.partition(":")
        print(
            f"{provider[:8]:8} {model[:28]:28} {int(row['calls']):5d} {int(row['failures']):8d} "
            f"{int(row.get('quota_pauses', 0)):5d} {int(row.get('waiting_network', 0)):7d} "
            f"{int(row.get('waiting_provider_busy', 0)):4d} "
            f"{int(row.get('waiting_provider_error', 0)):6d} "
            f"{int(row.get('waiting_timeout', 0)):7d} "
            f"{int(row.get('provider_switches', 0)):8d} {float(row['seconds']) / 60:7.2f}"
        )
    return 0


def cmd_models_report(
    *,
    project: str | None = None,
    since: str | None = None,
) -> int:
    project_path = Path(project).resolve() if project else None
    text = render_models_report(project=project_path, since=since)
    print(text, end="" if text.endswith("\n") else "\n")
    return 0


def cmd_models_import(projects: list[str]) -> int:
    count = import_usage_records([Path(project) for project in projects])
    noun = "record" if count == 1 else "records"
    print(f"Imported {count} {noun}.")
    return 0


def cmd_quota(runtime: Runtime, action: str, value: str | None = None) -> int:
    if action == "status":
        status = runtime.quota.status()
        print(f"policy: {status.policy}")
        print(f"reset_at: {status.reset_at or '(unknown)'} ({status.source})")
        print("waiting_tasks: " + (", ".join(status.waiting_tasks) if status.waiting_tasks else "(none)"))
        capacity = runtime.capacity.status()
        print(f"capacity_policy: {capacity.policy}")
        print(f"capacity_retry_minutes: {capacity.retry_interval_minutes:g}")
        print(
            "capacity_waiting_tasks: "
            + (", ".join(capacity.waiting_tasks) if capacity.waiting_tasks else "(none)")
        )
        return 0
    if action == "set-reset":
        if not value:
            print("set-reset requires an ISO-8601 timestamp", file=sys.stderr)
            return 2
        normalized = runtime.quota.set_reset_at(value)
        print(f"Quota reset override set to {normalized}")
        return 0
    if action == "clear-reset":
        runtime.quota.clear_reset_at()
        print("Quota reset override cleared; harness.yaml/unknown reset will be used")
        return 0
    print(f"Unknown quota action: {action}", file=sys.stderr)
    return 2


def cmd_plan_change_propose(
    runtime: Runtime, sprint_id: str, task_id: str, model_class: str | None
) -> int:
    if not runtime.config.plan_repair.enabled:
        print("Plan repair is disabled in harness.yaml", file=sys.stderr)
        return 2
    runtime.plan_changes.ensure_baseline(runtime.roadmap)
    task = _resolve_task(runtime, task_id)
    if task.sprint != sprint_id:
        print(f"{task_id} belongs to {task.sprint}, not {sprint_id}", file=sys.stderr)
        return 2
    rt = runtime.state.get(task_id)
    failure = runtime.failures.get(sprint_id)
    failure_summary = str((failure or {}).get("message") or rt.last_error or rt.review_feedback or "Task exhausted its normal path")
    workspace = Path(rt.worktree) if rt.worktree and Path(rt.worktree).exists() else runtime.root
    selected_model = model_class or runtime.config.plan_repair.planner_model_class
    if selected_model not in runtime.config.cursor.models:
        print(f"Unknown plan-repair model class {selected_model!r}", file=sys.stderr)
        return 2
    request = runtime.plan_changes.propose(
        roadmap=runtime.roadmap,
        runner=runtime.runner,
        sprint_id=sprint_id,
        task_id=task_id,
        workspace=workspace,
        failure_summary=failure_summary,
        timeout_minutes=runtime.config.plan_repair.timeout_minutes,
        model_class=selected_model,
        env=(runtime.environment.task_env(task, workspace) if workspace != runtime.root else None),
    )
    print(f"{request.id}: {request.kind} ({request.status})")
    print(runtime.plan_changes.markdown_path(request.id).relative_to(runtime.root))
    if request.status == "NO_PLAN_CHANGE":
        print("Planner says the existing plan is sufficient; use a focused expensive retry if appropriate.")
    else:
        print(f"Human gate opened. Review the proposal before applying {request.id}.")
    return 0


def cmd_plan_change_status(runtime: Runtime, request_id: str | None) -> int:
    rid = request_id or runtime.plan_changes.open_request_id()
    if not rid:
        print("No open plan change request.")
        return 0
    data = runtime.plan_changes.get(rid)
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def cmd_plan_change_apply(runtime: Runtime, request_id: str, affected: list[str]) -> int:
    if not runtime.worktrees.is_clean():
        print(
            "Commit the architecture/roadmap/task edits before applying the plan revision; "
            "the integration checkout must be clean.",
            file=sys.stderr,
        )
        return 2
    report = runtime.validator.validate()
    if not report.ok:
        print(report.render(), file=sys.stderr)
        return 2
    diff = runtime.plan_changes.apply(
        request_id, runtime.roadmap, affected_task_ids=affected or None
    )
    # A plan revision changes the base contract. Preserve any old unintegrated
    # work as a patch, then recreate affected worktrees from the revised HEAD.
    archive_dir = runtime.root / ".harness" / "change_requests"
    for task_id in diff.get("affected_tasks", []):
        task_rt = runtime.state.get(str(task_id))
        if task_rt.status == TaskStatus.DONE or not task_rt.worktree:
            continue
        worktree_path = Path(task_rt.worktree)
        if worktree_path.exists() and task_rt.base_ref:
            patch_path = archive_dir / f"{request_id}-{task_id}-pre-revision.patch"
            # Include untracked files in the forensic patch without committing
            # them: intent-to-add makes `git diff` render their full content.
            subprocess.run(["git", "add", "-N", "."], cwd=worktree_path, text=True, encoding="utf-8", errors="replace", capture_output=True)
            proc = subprocess.run(
                ["git", "diff", "--binary", task_rt.base_ref],
                cwd=worktree_path,
                text=True, encoding="utf-8", errors="replace",
                capture_output=True,
            )
            if proc.stdout:
                patch_path.write_text(proc.stdout, encoding="utf-8")
        if task_rt.branch and worktree_path.exists():
            try:
                runtime.worktrees.remove(worktree_path, task_rt.branch, delete_branch=True)
            except GitError as exc:
                print(f"WARNING: {exc}", file=sys.stderr)
        elif task_rt.branch:
            runtime.worktrees.delete_branch(task_rt.branch)
        task_rt.worktree = None
        task_rt.branch = None
        task_rt.base_ref = None
        task_rt.commit = None
    runtime.state.save()
    print(f"Applied {request_id}; plan revision is now {runtime.plan_changes.revision()}")
    print(json.dumps(diff, indent=2, sort_keys=True))
    print("Resume the affected sprint; DONE work will not rerun, and listed DONE tasks will be revalidated first.")
    return 0


def cmd_plan_change_reject(runtime: Runtime, request_id: str) -> int:
    runtime.plan_changes.reject(request_id)
    print(f"Rejected {request_id}; existing blocked-task recovery remains available.")
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Small task-oriented Cursor coding-agent harness")
    p.add_argument("--root", default=".", help="Git repository root")
    p.add_argument("--roadmap", default="tasks/roadmap.yaml", help="Roadmap YAML path relative to root")
    p.add_argument("--config", default="harness.yaml", help="Harness config path relative to root")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("status")
    s.add_argument("sprint", nargs="?")

    watch = sub.add_parser("watch")
    watch.add_argument("sprint", nargs="?")

    g = sub.add_parser("graph")
    g.add_argument("sprint")

    r = sub.add_parser("run")
    r.add_argument("sprint")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--no-board", action="store_true")

    rs = sub.add_parser("resume")
    rs.add_argument("sprint")
    rs.add_argument("--no-board", action="store_true")

    rel = sub.add_parser("release")
    rel.add_argument("sprint", help="Sprint id (release) or held task id (hold/release)")
    rel.add_argument("--fresh-budget", action="store_true")

    hold = sub.add_parser("hold")
    hold.add_argument("task")

    rt = sub.add_parser("run-task")
    rt.add_argument("task")
    rt.add_argument("--model-class")
    rt.add_argument("--fresh-budget", action="store_true")

    fail = sub.add_parser("failure")
    fail.add_argument("sprint")

    rb = sub.add_parser("rerun-blocker")
    rb.add_argument("sprint")
    rb.add_argument("--model-class", default=None)
    rb.add_argument("--task", default=None)

    ry = sub.add_parser("retry")
    ry.add_argument("task")

    ub = sub.add_parser("unblock")
    ub.add_argument("task")

    reset = sub.add_parser("reset-task")
    reset.add_argument("task")

    v = sub.add_parser("verify")
    v.add_argument("task")

    rv = sub.add_parser("review")
    rv.add_argument("task")
    rv.add_argument("--timeout-minutes", type=int, default=20)

    ins = sub.add_parser("inspect")
    ins.add_argument("task")

    clean = sub.add_parser("cleanup")
    clean.add_argument("--stale-worktrees", action="store_true")
    clean.add_argument("--completed", action="store_true")

    man = sub.add_parser("manifest")
    man.add_argument("sprint")

    sub.add_parser("usage")
    val = sub.add_parser("validate")
    val.add_argument("sprint", nargs="?")

    brief_check = sub.add_parser("brief-check")
    brief_check.add_argument("task")
    brief_check.add_argument("--timeout-minutes", type=int, default=10)

    init_p = sub.add_parser("init")
    init_p.add_argument("--force", action="store_true")
    init_p.add_argument("--public", action="store_true")
    init_p.add_argument("--no-github", action="store_true")

    limits_p = sub.add_parser("limits")
    limits_p.add_argument("--json", action="store_true")

    sub.add_parser("setup-machine")

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--full", action="store_true")

    quota = sub.add_parser("quota")
    quota_sub = quota.add_subparsers(dest="quota_action", required=True)
    quota_sub.add_parser("status")
    qset = quota_sub.add_parser("set-reset")
    qset.add_argument("value")
    quota_sub.add_parser("clear-reset")

    pc = sub.add_parser("plan-change")
    pc_sub = pc.add_subparsers(dest="plan_action", required=True)
    pcp = pc_sub.add_parser("propose")
    pcp.add_argument("sprint")
    pcp.add_argument("--task", required=True)
    pcp.add_argument("--model-class", default=None)
    pcs = pc_sub.add_parser("status")
    pcs.add_argument("request", nargs="?")
    pca = pc_sub.add_parser("apply")
    pca.add_argument("request")
    pca.add_argument("--affected", action="append", default=[])
    pcr = pc_sub.add_parser("reject")
    pcr.add_argument("request")

    at = sub.add_parser("add-task")
    at.add_argument("sprint")
    at.add_argument("brief")
    at.add_argument("--stage", default="auto", choices=["auto", "current", "next"])

    models = sub.add_parser("models")
    models_sub = models.add_subparsers(dest="models_action", required=True)
    models_report = models_sub.add_parser("report")
    models_report.add_argument(
        "--project",
        default=None,
        help="Limit to one project root path",
    )
    models_report.add_argument(
        "--since",
        default=None,
        help="Include records on or after YYYY-MM-DD",
    )
    models_import = models_sub.add_parser("import")
    models_import.add_argument("projects", nargs="+", metavar="PROJECT")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    root = Path(args.root).resolve()
    if args.command == "init":
        return run_init(
            root,
            force=args.force,
            public=args.public,
            no_github=args.no_github,
        )
    if args.command == "limits":
        return run_limits(
            as_json=args.json,
            root=root,
            config_path=(root / args.config).resolve(),
        )
    if args.command == "setup-machine":
        return run_setup_machine()
    if args.command == "models":
        if args.models_action == "report":
            return cmd_models_report(
                project=getattr(args, "project", None),
                since=getattr(args, "since", None),
            )
        if args.models_action == "import":
            return cmd_models_import(args.projects)
        print(f"Unknown models action: {args.models_action}", file=sys.stderr)
        return 2
    roadmap_path = (root / args.roadmap).resolve()
    config_path = (root / args.config).resolve()
    try:
        runtime = build_runtime(root, roadmap_path, config_path)
        if args.command == "status":
            # Tests may stub `cmd_status` without the newer keyword.
            if "config" in inspect.signature(cmd_status).parameters:
                return cmd_status(
                    runtime.roadmap,
                    runtime.state,
                    args.sprint,
                    runtime.root / ".harness",
                    config=getattr(runtime, "config", None),
                )
            return cmd_status(
                runtime.roadmap,
                runtime.state,
                args.sprint,
                runtime.root / ".harness",
            )
        if args.command == "watch":
            return cmd_watch(runtime, args.sprint)
        if args.command == "add-task":
            return cmd_add_task(
                runtime,
                args.sprint,
                (root / args.brief).resolve(),
                stage_choice=args.stage,
            )
        if args.command == "hold":
            return cmd_hold(runtime, args.task)
        if args.command == "release":
            if args.sprint not in runtime.roadmap.sprints:
                return cmd_release_task(runtime, args.sprint, fresh_budget=args.fresh_budget)
            if args.fresh_budget:
                print("--fresh-budget applies to a held task, not a sprint.", file=sys.stderr)
                return 2
            return cmd_release(runtime, args.sprint)
        if args.command == "graph":
            return cmd_graph(runtime.roadmap, args.sprint)
        if args.command in {"run", "resume"}:
            return cmd_run(
                runtime,
                args.sprint,
                getattr(args, "dry_run", False),
                no_board=bool(getattr(args, "no_board", False)),
            )
        if args.command == "run-task":
            return cmd_run_task(
                runtime, args.task, model_class=args.model_class, fresh_budget=args.fresh_budget
            )
        if args.command == "failure":
            return cmd_failure(runtime, args.sprint)
        if args.command == "rerun-blocker":
            return cmd_rerun_blocker(runtime, args.sprint, args.model_class, args.task)
        if args.command == "retry":
            return cmd_retry(runtime.state, args.task)
        if args.command == "unblock":
            return cmd_unblock(runtime.state, args.task)
        if args.command == "reset-task":
            return cmd_reset_task(runtime, args.task)
        if args.command == "verify":
            return cmd_verify(runtime, args.task)
        if args.command == "review":
            return cmd_review(runtime, args.task, args.timeout_minutes)
        if args.command == "inspect":
            return cmd_inspect(runtime, args.task)
        if args.command == "cleanup":
            return cmd_cleanup(
                runtime,
                stale_worktrees=args.stale_worktrees,
                completed=args.completed,
            )
        if args.command == "manifest":
            return cmd_manifest(runtime, args.sprint)
        if args.command == "usage":
            return cmd_usage(runtime)
        if args.command == "validate":
            return cmd_validate(runtime, args.sprint)
        if args.command == "brief-check":
            return cmd_brief_check(runtime, args.task, args.timeout_minutes)
        if args.command == "doctor":
            return cmd_doctor(runtime, full=args.full)
        if args.command == "quota":
            return cmd_quota(runtime, args.quota_action, getattr(args, "value", None))
        if args.command == "plan-change":
            if args.plan_action == "propose":
                return cmd_plan_change_propose(runtime, args.sprint, args.task, args.model_class)
            if args.plan_action == "status":
                return cmd_plan_change_status(runtime, args.request)
            if args.plan_action == "apply":
                return cmd_plan_change_apply(runtime, args.request, args.affected)
            if args.plan_action == "reject":
                return cmd_plan_change_reject(runtime, args.request)
    except (RoadmapError, RuntimeError, OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    return 0
