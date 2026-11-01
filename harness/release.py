from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import HarnessConfig
from .logging_utils import utc_now
from .models import SprintSpec, TaskStatus
from .state import StateStore
from .verifier import Verifier


@dataclass(slots=True)
class ReleaseResult:
    ok: bool
    skipped: bool = False
    failed_command: str | None = None
    exit_code: int | str | None = None


def sprint_is_complete(state: StateStore, sprint: SprintSpec) -> bool:
    return all(state.get(task.id).status == TaskStatus.DONE for task in sprint.tasks.values())


def _release_meta_key(sprint_id: str, suffix: str) -> str:
    return f"{sprint_id}.release.{suffix}"


def format_release_status(state: StateStore, sprint_id: str) -> str | None:
    status = state.get_meta(_release_meta_key(sprint_id, "status"))
    if status == "done":
        completed_at = state.get_meta(_release_meta_key(sprint_id, "completed_at"), "")
        return f"release: done {completed_at}"
    if status == "failed":
        command = state.get_meta(_release_meta_key(sprint_id, "failed_command"), "")
        exit_code = state.get_meta(_release_meta_key(sprint_id, "failed_exit_code"), "")
        return (
            f"release: FAILED ({command}, exit {exit_code}); "
            f"retry with: harness release {sprint_id}"
        )
    return None


class ReleaseRunner:
    def __init__(self, config: HarnessConfig, repo_root: Path, verifier: Verifier):
        self.config = config
        self.repo_root = repo_root
        self.verifier = verifier
        self.log_dir = verifier.log_dir

    def run(self, sprint_id: str) -> ReleaseResult:
        commands = self.config.release.commands
        if not commands:
            return ReleaseResult(True, skipped=True)

        timeout_seconds = max(1, self.config.release.timeout_minutes) * 60
        ok, _summary, failed_command, exit_code = self.verifier.run_shell_commands(
            commands,
            self.repo_root,
            timeout_seconds=timeout_seconds,
            log_name=f"{sprint_id}-release.log",
        )
        if ok:
            return ReleaseResult(True)
        return ReleaseResult(False, failed_command=failed_command, exit_code=exit_code)

    def run_and_persist(self, state: StateStore, sprint_id: str) -> ReleaseResult:
        result = self.run(sprint_id)
        if result.skipped:
            return result
        if result.ok:
            state.set_meta(_release_meta_key(sprint_id, "status"), "done")
            state.set_meta(_release_meta_key(sprint_id, "completed_at"), utc_now())
            state.set_meta(_release_meta_key(sprint_id, "failed_command"), None)
            state.set_meta(_release_meta_key(sprint_id, "failed_exit_code"), None)
        else:
            state.set_meta(_release_meta_key(sprint_id, "status"), "failed")
            state.set_meta(_release_meta_key(sprint_id, "failed_command"), result.failed_command)
            state.set_meta(_release_meta_key(sprint_id, "failed_exit_code"), result.exit_code)
        return result

    def maybe_run_after_sprint_complete(self, state: StateStore, sprint_id: str, *, dry_run: bool) -> None:
        if dry_run or not self.config.release.commands:
            return
        # Automatic release runs once. A failed attempt is retried with `harness release`.
        if state.get_meta(_release_meta_key(sprint_id, "status")) is not None:
            return
        self.run_and_persist(state, sprint_id)
