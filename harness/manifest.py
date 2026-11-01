from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import HarnessConfig
from .models import SprintSpec
from .process_utils import prepare_external_argv, resolve_codex_argv
from .state import StateStore


def _sha256_file(path: Path) -> str | None:
    if not path.exists() or not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_hash(root: Path, paths: list[Path]) -> str | None:
    existing = sorted((p for p in paths if p.exists() and p.is_file()), key=lambda p: p.as_posix())
    if not existing:
        return None
    digest = hashlib.sha256()
    for path in existing:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update((_sha256_file(path) or "").encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _safe_command(args: list[str], cwd: Path) -> str | None:
    try:
        proc = subprocess.run(args, cwd=cwd, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or proc.stderr.strip() or None


def _plan_revision(state: StateStore) -> int:
    value = state.get_meta("plan.revision", 1)
    if isinstance(value, bool):
        return 1
    if isinstance(value, (int, float, str, bytes, bytearray)):
        try:
            return int(value or 1)
        except (TypeError, ValueError):
            pass
    raise RuntimeError("Invalid plan.revision state; expected an integer")


class RunManifestManager:
    def __init__(self, root: Path, runtime_root: Path, state: StateStore):
        self.root = root.resolve()
        self.runtime_root = runtime_root
        self.state = state
        self.runs_root = runtime_root / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)

    def _spec_files(self, sprint: SprintSpec) -> list[Path]:
        return [self.root / task.file for task in sprint.tasks.values()]

    def start_or_resume(
        self,
        *,
        sprint: SprintSpec,
        roadmap_path: Path,
        config_path: Path,
        config: HarnessConfig,
        integration_branch: str,
        started_from_commit: str,
    ) -> Path:
        key = f"{sprint.id}.active_run_id"
        run_id = self.state.get_meta(key)
        if isinstance(run_id, str):
            existing = self.runs_root / run_id / "manifest.json"
            if existing.exists():
                existing_payload = json.loads(existing.read_text(encoding="utf-8"))
                existing_payload["status"] = "RUNNING"
                existing_payload.setdefault("resumed_at", []).append(datetime.now(timezone.utc).isoformat())
                current_revision = _plan_revision(self.state)
                if existing_payload.get("plan_revision") != current_revision:
                    existing_payload.setdefault("plan_revision_history", []).append({
                        "at": datetime.now(timezone.utc).isoformat(),
                        "from": existing_payload.get("plan_revision"),
                        "to": current_revision,
                    })
                    existing_payload["plan_revision"] = current_revision
                existing.write_text(json.dumps(existing_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
                return existing

        # New run: try Codex again for every task. Resume of an active run preserves
        # a task's prior Codex->Cursor switch caused by quota/auth/model unavailability.
        for task_id in sprint.tasks:
            self.state.set_meta(f"{sprint.id}.{task_id}.provider.codex.disabled", None)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        run_id = f"{sprint.id}-{stamp}-{uuid.uuid4().hex[:8]}"
        run_dir = self.runs_root / run_id
        run_dir.mkdir(parents=True, exist_ok=False)

        architecture_files = []
        architecture = self.root / "ARCHITECTURE.md"
        if architecture.exists():
            architecture_files.append(architecture)
        docs_arch = self.root / "docs" / "architecture"
        if docs_arch.exists():
            architecture_files.extend(p for p in docs_arch.rglob("*") if p.is_file())

        assignment = self.root / "docs" / "assignment.md"
        agents = self.root / "AGENTS.md"
        cursor_version = _safe_command([config.cursor.command, "--version"], self.root)
        codex_version = _safe_command(prepare_external_argv(resolve_codex_argv(config.codex.command, "--version")), self.root) if config.codex.enabled else None

        payload: dict[str, Any] = {
            "run_id": run_id,
            "project": roadmap_path.parent.parent.name if roadmap_path.parent.parent else self.root.name,
            "sprint": sprint.id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "started_from_commit": started_from_commit,
            "integration_branch": integration_branch,
            "harness_version": __version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cursor_version": cursor_version,
            "codex_version": codex_version,
            "models": {"codex": config.codex.implementation_sequence, "cursor": config.cursor.models},
            "plan_revision": _plan_revision(self.state),
            "hashes": {
                "roadmap": _sha256_file(roadmap_path),
                "harness_config": _sha256_file(config_path),
                "agents": _sha256_file(agents),
                "assignment": _sha256_file(assignment),
                "architecture": _tree_hash(self.root, architecture_files),
                "task_files": _tree_hash(self.root, self._spec_files(sprint)),
            },
            "status": "RUNNING",
        }
        path = run_dir / "manifest.json"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.state.set_meta(key, run_id)
        return path

    def set_status(self, sprint_id: str, status: str) -> None:
        key = f"{sprint_id}.active_run_id"
        run_id = self.state.get_meta(key)
        if not isinstance(run_id, str):
            return
        path = self.runs_root / run_id / "manifest.json"
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["status"] = status
        payload["status_updated_at"] = datetime.now(timezone.utc).isoformat()
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def finish(self, sprint_id: str, *, success: bool) -> None:
        key = f"{sprint_id}.active_run_id"
        run_id = self.state.get_meta(key)
        if not isinstance(run_id, str):
            return
        path = self.runs_root / run_id / "manifest.json"
        if not path.exists():
            return
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["finished_at"] = datetime.now(timezone.utc).isoformat()
        payload["status"] = "COMPLETED" if success else "BLOCKED"
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if success:
            self.state.set_meta(key, None)

    def current_path(self, sprint_id: str) -> Path | None:
        run_id = self.state.get_meta(f"{sprint_id}.active_run_id")
        if isinstance(run_id, str):
            path = self.runs_root / run_id / "manifest.json"
            if path.exists():
                return path
        candidates = sorted(self.runs_root.glob(f"{sprint_id}-*/manifest.json"))
        return candidates[-1] if candidates else None
