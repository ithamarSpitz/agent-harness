from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .config import HarnessConfig
from .git_worktree import WorktreeManager
from .process_utils import locate_codex_executable, locate_executable, prepare_external_argv, resolve_codex_argv, split_command


@dataclass(slots=True)
class DoctorCheck:
    name: str
    ok: bool
    detail: str
    required: bool = True


class Doctor:
    @staticmethod
    def _version_tuple(text: str) -> tuple[int, ...] | None:
        match = re.search(r"(?<!\d)(\d+)(?:\.(\d+))?(?:\.(\d+))?", text)
        if not match:
            return None
        return tuple(int(part or 0) for part in match.groups())

    def __init__(self, root: Path, config: HarnessConfig, worktrees: WorktreeManager):
        self.root = root
        self.config = config
        self.worktrees = worktrees

    @staticmethod
    def _command_version(command: str) -> tuple[bool, str]:
        parts = split_command(command)
        if not parts:
            return False, "missing"
        path = locate_executable(parts[0])
        if not path:
            return False, "missing"
        for suffix in (["--version"], ["version"]):
            try:
                proc = subprocess.run(
                    prepare_external_argv([*parts, *suffix]),
                    text=True, encoding="utf-8", errors="replace",
                    capture_output=True,
                    timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            text = (proc.stdout or proc.stderr).strip()
            if proc.returncode == 0:
                return True, text or path
        return True, path

    def run(self, *, full: bool = False) -> list[DoctorCheck]:
        checks: list[DoctorCheck] = []
        checks.append(DoctorCheck("python", sys.version_info >= (3, 11), sys.version.split()[0]))
        git_ok, git_detail = self._command_version("git")
        checks.append(DoctorCheck("git", git_ok, git_detail))

        cursor_parts = split_command(self.config.cursor.command) if self.config.cursor.enabled else []
        cursor_path = locate_executable(cursor_parts[0]) if cursor_parts else None
        if self.config.cursor.enabled:
            checks.append(DoctorCheck("cursor-agent", cursor_path is not None, cursor_path or "missing"))
        else:
            checks.append(DoctorCheck("cursor-agent", True, "disabled by configuration", required=False))
        try:
            self.worktrees.ensure_repo()
            checks.append(DoctorCheck("git-root", True, str(self.root)))
        except Exception as exc:
            checks.append(DoctorCheck("git-root", False, str(exc)))

        if self.config.cursor.enabled and cursor_path:
            ok, detail = self._command_version(self.config.cursor.command)
            checks.append(DoctorCheck("cursor-version", ok, detail))

        codex_parts = split_command(self.config.codex.command) if self.config.codex.enabled else []
        codex_path = locate_codex_executable(codex_parts[0]) if codex_parts else None
        if self.config.codex.enabled:
            codex_required = not self.config.cursor.enabled
            checks.append(DoctorCheck(
                "codex-cli",
                codex_path is not None,
                codex_path or ("missing; Cursor fallback remains available" if self.config.cursor.enabled else "missing; Cursor fallback is disabled"),
                required=codex_required,
            ))
            if codex_path:
                try:
                    version_proc = subprocess.run(
                        prepare_external_argv(resolve_codex_argv(self.config.codex.command, "--version")), cwd=self.root,
                        text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=15,
                    )
                    version_text = ((version_proc.stdout or "") + ("\n" + version_proc.stderr if version_proc.stderr else "")).strip()
                    checks.append(DoctorCheck("codex-version", version_proc.returncode == 0, version_text[:300] or codex_path, required=not self.config.cursor.enabled))
                    proc = subprocess.run(
                        prepare_external_argv(resolve_codex_argv(self.config.codex.command, "login", "status")), cwd=self.root,
                        text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=20,
                    )
                    output = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
                    required = self.config.codex.auth_required_substring
                    logged = proc.returncode == 0 and (not required or required.casefold() in output.casefold())
                    checks.append(DoctorCheck(
                        "codex-chatgpt-auth",
                        logged,
                        output[:300] or ("not authenticated; Cursor fallback remains available" if self.config.cursor.enabled else "not authenticated; Cursor fallback is disabled"),
                        required=not self.config.cursor.enabled,
                    ))
                except (OSError, subprocess.TimeoutExpired) as exc:
                    checks.append(DoctorCheck("codex-chatgpt-auth", False, str(exc), required=not self.config.cursor.enabled))

        if not full:
            return checks

        try:
            clean = self.worktrees.is_clean()
            checks.append(DoctorCheck("repo-clean", clean, "clean" if clean else "uncommitted changes present"))
        except Exception as exc:
            checks.append(DoctorCheck("repo-clean", False, str(exc)))

        usage = shutil.disk_usage(self.root)
        free_gb = usage.free / (1024 ** 3)
        checks.append(
            DoctorCheck(
                "disk-free",
                free_gb >= self.config.doctor.min_free_disk_gb,
                f"{free_gb:.2f} GiB free (minimum {self.config.doctor.min_free_disk_gb:.2f})",
            )
        )

        command_details: dict[str, tuple[bool, str]] = {}
        for command in self.config.doctor.required_commands:
            ok, detail = self._command_version(command)
            command_details[command] = (ok, detail)
            checks.append(DoctorCheck(f"command:{command}", ok, detail))

        for command, minimum in self.config.doctor.minimum_versions.items():
            ok, detail = command_details.get(command, self._command_version(command))
            current_tuple = self._version_tuple(detail) if ok else None
            minimum_tuple = self._version_tuple(minimum)
            version_ok = bool(ok and current_tuple is not None and minimum_tuple is not None and current_tuple >= minimum_tuple)
            checks.append(
                DoctorCheck(
                    f"version:{command}",
                    version_ok,
                    f"found {detail!r}; minimum {minimum}",
                )
            )

        for name in self.config.doctor.required_env:
            value = os.environ.get(name)
            checks.append(DoctorCheck(f"env:{name}", bool(value), "set" if value else "missing"))

        if self.config.environment.docker_enabled:
            compose_parts = split_command(self.config.environment.compose_command)
            if not compose_parts:
                checks.append(DoctorCheck("docker-compose", False, "empty environment.compose_command"))
            else:
                executable = shutil.which(compose_parts[0])
                if executable is None:
                    checks.append(DoctorCheck("docker-compose", False, f"missing executable: {compose_parts[0]}"))
                else:
                    try:
                        proc = subprocess.run(
                            prepare_external_argv([*compose_parts, "version"]),
                            cwd=self.root,
                            text=True, encoding="utf-8", errors="replace",
                            capture_output=True,
                            timeout=20,
                        )
                        detail = ((proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")).strip()
                        checks.append(
                            DoctorCheck(
                                "docker-compose",
                                proc.returncode == 0,
                                detail[:300] or executable,
                            )
                        )
                    except (OSError, subprocess.TimeoutExpired) as exc:
                        checks.append(DoctorCheck("docker-compose", False, str(exc)))
            for compose_file in self.config.environment.compose_files:
                path = (self.root / compose_file).resolve()
                checks.append(
                    DoctorCheck(
                        f"compose-file:{compose_file}",
                        path.exists(),
                        str(path) if path.exists() else "missing",
                    )
                )

        if self.config.cursor.enabled and cursor_path and self.config.doctor.check_cursor_models:
            try:
                proc = subprocess.run(
                    prepare_external_argv([*cursor_parts, "models"]),
                    cwd=self.root,
                    text=True, encoding="utf-8", errors="replace",
                    capture_output=True,
                    timeout=30,
                )
                output = (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
                checks.append(
                    DoctorCheck(
                        "cursor-models/auth",
                        proc.returncode == 0,
                        "model listing succeeded" if proc.returncode == 0 else output.strip()[:300],
                    )
                )
                if proc.returncode == 0:
                    for model_class, model in self.config.cursor.models.items():
                        if not model:
                            continue
                        checks.append(
                            DoctorCheck(
                                f"model:{model_class}",
                                model.lower() in output.lower(),
                                model,
                            )
                        )
            except subprocess.TimeoutExpired:
                checks.append(DoctorCheck("cursor-models/auth", False, "agent models timed out"))
            except OSError as exc:
                checks.append(DoctorCheck("cursor-models/auth", False, str(exc)))

        return checks

    @staticmethod
    def render(checks: list[DoctorCheck]) -> str:
        lines: list[str] = []
        for check in checks:
            status = "OK" if check.ok else ("FAIL" if check.required else "WARN")
            lines.append(f"{status:4} {check.name:22} {check.detail}")
        return "\n".join(lines)
