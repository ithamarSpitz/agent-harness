from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from .config import CodeMapConfig
from .logging_utils import write_log
from .models import TaskSpec
from .process_utils import prepare_external_argv, split_command, stop_process_tree


class CodeMapBuilder:
    def __init__(self, config: CodeMapConfig, runtime_root: Path, log_dir: Path):
        self.config = config
        self.runtime_root = runtime_root
        self.log_dir = log_dir

    def requested(self, task: TaskSpec) -> bool:
        task_setting = self.config.default_for_tasks if task.code_map is None else task.code_map
        return self.config.enabled and task_setting

    def _warn(self, task: TaskSpec, message: str) -> None:
        write_log(self.log_dir, f"{task.id}-code-map.log", f"WARNING: {message}\n")

    def prepare(
        self,
        task: TaskSpec,
        worktree: Path,
        base_ref: str,
        *,
        env: dict[str, str] | None = None,
        allow_build: bool = True,
    ) -> Path | None:
        if not self.requested(task):
            return None

        output_dir = worktree / "graphify-out"
        build_attempted = False
        try:
            resolved = subprocess.run(
                ["git", "rev-parse", f"{base_ref}^{{commit}}"],
                cwd=worktree,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=15,
            )
            if resolved.returncode != 0:
                self._warn(task, f"code map skipped: could not resolve base commit {base_ref!r}")
                return None
            base_sha = resolved.stdout.strip()
            cache_path = (self.runtime_root / "code-map" / base_sha / "graph.json").resolve()
            if cache_path.is_file():
                return cache_path
            if not allow_build:
                return None

            command = split_command(self.config.command)
            if not command:
                self._warn(task, "code map skipped: configured command is empty; install with pip install graphifyy")
                return None
            build_attempted = True
            proc = subprocess.Popen(
                prepare_external_argv([*command, "update", ".", "--no-cluster"]),
                cwd=worktree,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                start_new_session=(os.name != "nt"),
                creationflags=(
                    getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                    if os.name == "nt" else 0
                ),
            )
            try:
                stdout, stderr = proc.communicate(
                    timeout=max(0.01, float(self.config.build_timeout_minutes)) * 60
                )
            except subprocess.TimeoutExpired:
                stop_process_tree(proc)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
                self._warn(
                    task,
                    f"code map build timed out after {self.config.build_timeout_minutes} minutes",
                )
                return None
            source = output_dir / "graph.json"
            if proc.returncode != 0 or not source.is_file():
                detail = (stderr or stdout).strip().splitlines()
                suffix = f": {detail[-1][:300]}" if detail else ""
                self._warn(task, f"code map build failed (exit {proc.returncode}){suffix}")
                return None

            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp_name: str | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    dir=cache_path.parent, prefix="graph.json.", suffix=".tmp", delete=False
                ) as temp_file:
                    temp_name = temp_file.name
                shutil.copyfile(source, temp_name)
                os.replace(temp_name, cache_path)
                temp_name = None
            finally:
                if temp_name is not None:
                    Path(temp_name).unlink(missing_ok=True)
            return cache_path
        except subprocess.TimeoutExpired as exc:
            self._warn(task, f"code map unavailable: {exc}")
            return None
        except (OSError, ValueError) as exc:
            self._warn(task, f"code map unavailable: {exc}; install with pip install graphifyy")
            return None
        finally:
            if build_attempted:
                shutil.rmtree(output_dir, ignore_errors=True)
