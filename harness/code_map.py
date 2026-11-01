from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path

from .config import CodeMapConfig
from .logging_utils import write_log
from .models import TaskSpec
from .process_utils import prepare_external_argv, split_command, stop_process_tree

_GRAPHIFY_AVAILABLE: bool | None = None
_GRAPHIFY_PROBE_LOCK = threading.Lock()
_PROJECT_MAP_LOCK = threading.Lock()

FALLBACK_RULES = (
    'Use `graphify query "<question>" --budget 500`, `graphify explain "<symbol or file>"` '
    'and `graphify path "<A>" "<B>"` to find code and see what depends on what. '
    "Read only the source lines the map points to. After you change code, run "
    "`graphify update .` so the map shows your changes."
)


def reset_graphify_availability_cache() -> None:
    global _GRAPHIFY_AVAILABLE
    with _GRAPHIFY_PROBE_LOCK:
        _GRAPHIFY_AVAILABLE = None


def graphify_command_available(config: CodeMapConfig) -> bool:
    global _GRAPHIFY_AVAILABLE
    with _GRAPHIFY_PROBE_LOCK:
        if _GRAPHIFY_AVAILABLE is not None:
            return _GRAPHIFY_AVAILABLE
        parts = split_command(config.command)
        if not parts:
            _GRAPHIFY_AVAILABLE = False
            return False
        try:
            proc = subprocess.run(
                prepare_external_argv([*parts, "--help"]),
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=20,
            )
            _GRAPHIFY_AVAILABLE = proc.returncode == 0
        except (OSError, subprocess.TimeoutExpired, ValueError):
            _GRAPHIFY_AVAILABLE = False
        return _GRAPHIFY_AVAILABLE


def graphify_rules_text() -> str:
    spec = importlib.util.find_spec("graphify")
    if spec is None or not spec.origin:
        return FALLBACK_RULES
    rules = Path(spec.origin).resolve().parent / "always_on" / "agents-md.md"
    if rules.is_file():
        return rules.read_text(encoding="utf-8")
    return FALLBACK_RULES


def ensure_graphify_out_excluded(repo_root: Path) -> None:
    result = subprocess.run(
        ["git", "rev-parse", "--git-common-dir"],
        cwd=repo_root,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=15,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or "could not resolve git common directory")
    common = Path(result.stdout.strip())
    if not common.is_absolute():
        common = (repo_root / common).resolve()
    exclude = common / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    line = "graphify-out/"
    existing = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
    if line in existing.splitlines():
        return
    with exclude.open("a", encoding="utf-8") as handle:
        if existing and not existing.endswith("\n"):
            handle.write("\n")
        handle.write(f"{line}\n")


class CodeMapBuilder:
    def __init__(
        self,
        config: CodeMapConfig,
        runtime_root: Path,
        log_dir: Path,
        repo_root: Path,
    ):
        self.config = config
        self.runtime_root = runtime_root.resolve()
        self.log_dir = log_dir.resolve()
        self.repo_root = repo_root.resolve()
        self._map_root = self.runtime_root / "code-map"
        self._source = self._map_root / "source"
        self._commit_file = self._map_root / "commit.txt"

    def _warn(self, message: str, task: TaskSpec | None = None) -> None:
        name = f"{task.id}-code-map.log" if task else "code-map.log"
        write_log(self.log_dir, name, f"WARNING: {message}\n")

    def _cleanup_legacy_sha_caches(self) -> None:
        if not self._map_root.is_dir():
            return
        sha_dir = re.compile(r"^[0-9a-f]{7,40}$", re.IGNORECASE)
        for child in self._map_root.iterdir():
            if child.name in {"source", "commit.txt"}:
                continue
            if child.is_dir() and sha_dir.match(child.name):
                shutil.rmtree(child, ignore_errors=True)

    def _read_recorded_commit(self) -> str | None:
        if not self._commit_file.is_file():
            return None
        value = self._commit_file.read_text(encoding="utf-8").strip()
        return value or None

    def _write_recorded_commit(self, commit: str) -> None:
        self._map_root.mkdir(parents=True, exist_ok=True)
        self._commit_file.write_text(commit + "\n", encoding="utf-8")

    def _project_graph_ready(self) -> bool:
        return (self._source / "graphify-out" / "graph.json").is_file()

    def _ensure_source_worktree(self, base_commit: str) -> bool:
        self._map_root.mkdir(parents=True, exist_ok=True)
        if not self._source.exists():
            result = subprocess.run(
                ["git", "worktree", "add", "--detach", str(self._source), base_commit],
                cwd=self.repo_root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=120,
            )
            if result.returncode != 0:
                detail = (result.stderr or result.stdout).strip()
                self._warn(f"code map skipped: could not create source worktree: {detail[:300]}")
                return False
            return True
        result = subprocess.run(
            ["git", "checkout", "--detach", base_commit],
            cwd=self._source,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            self._warn(f"code map skipped: could not checkout {base_commit}: {detail[:300]}")
            return False
        return True

    def _run_update(self, cwd: Path, *, env: dict[str, str] | None) -> bool:
        command = split_command(self.config.command)
        if not command:
            self._warn("code map skipped: configured command is empty; pip install graphifyy")
            return False
        proc = subprocess.Popen(
            prepare_external_argv([*command, "update", ".", "--no-cluster"]),
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=(os.name != "nt"),
            creationflags=(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
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
                f"code map build timed out after {self.config.build_timeout_minutes} minutes"
            )
            return False
        graph = cwd / "graphify-out" / "graph.json"
        if proc.returncode != 0 or not graph.is_file():
            detail = (stderr or stdout).strip().splitlines()
            suffix = f": {detail[-1][:300]}" if detail else ""
            self._warn(f"code map build failed (exit {proc.returncode}){suffix}")
            return False
        return True

    def ensure_project_map(self, base_commit: str, *, env: dict[str, str] | None = None) -> bool:
        with _PROJECT_MAP_LOCK:
            self._cleanup_legacy_sha_caches()
            if not graphify_command_available(self.config):
                self._warn("code map skipped: Graphify command not available; pip install graphifyy")
                return False
            recorded = self._read_recorded_commit()
            if recorded == base_commit and self._project_graph_ready():
                return True
            if not self._ensure_source_worktree(base_commit):
                return False
            if not self._run_update(self._source, env=env):
                return False
            self._write_recorded_commit(base_commit)
            return True

    def offer_to_worktree(self, task: TaskSpec, worktree: Path) -> bool:
        if not graphify_command_available(self.config):
            return False
        if not self._project_graph_ready():
            self._warn("code map skipped: project map is not available", task)
            return False
        try:
            ensure_graphify_out_excluded(self.repo_root)
            source_out = self._source / "graphify-out"
            dest = worktree / "graphify-out"
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(source_out, dest)
            (dest / "AGENTS-graphify.md").write_text(graphify_rules_text(), encoding="utf-8")
            return True
        except OSError as exc:
            self._warn(f"code map unavailable: {exc}; install with pip install graphifyy", task)
            return False
