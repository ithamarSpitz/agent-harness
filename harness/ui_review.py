from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .agent_runner import AgentResult
from .config import UIReviewConfig
from .logging_utils import write_log
from .process_utils import prepare_external_argv


@dataclass(frozen=True, slots=True)
class WorktreeSnapshot:
    status: str
    diff_hash: str


def capture_worktree_snapshot(workspace: Path) -> WorktreeSnapshot:
    status_proc = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=workspace,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
    )
    status = status_proc.stdout if status_proc.returncode == 0 else ""
    diff_proc = subprocess.run(
        ["git", "diff", "HEAD"],
        cwd=workspace,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        timeout=30,
    )
    diff_body = diff_proc.stdout if diff_proc.returncode == 0 else ""
    diff_hash = hashlib.sha256(diff_body.encode("utf-8")).hexdigest()
    return WorktreeSnapshot(status=status, diff_hash=diff_hash)


def _status_paths(status: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for line in status.splitlines():
        if not line.strip():
            continue
        entry = line[3:].strip() if len(line) > 3 else line.strip()
        if not entry:
            continue
        names = [part.strip() for part in entry.split(" -> ", 1)] if " -> " in entry else [entry]
        for name in names:
            if name:
                entries[name] = line
    return entries


def worktree_changed_paths(before: WorktreeSnapshot, after: WorktreeSnapshot) -> list[str]:
    if before.status == after.status and before.diff_hash == after.diff_hash:
        return []
    before_entries = _status_paths(before.status)
    after_entries = _status_paths(after.status)
    paths = [
        path
        for path in sorted(set(before_entries) | set(after_entries))
        if before_entries.get(path) != after_entries.get(path)
    ]
    if not paths:
        if before.diff_hash != after.diff_hash:
            paths.append("(tracked content changed)")
        else:
            paths.append("(worktree status changed)")
    return paths


def fail_review_for_worktree_edits(result: AgentResult, changed_paths: list[str]) -> AgentResult:
    listing = "\n".join(f"- {path}" for path in changed_paths)
    finding = f"Reviewer changed files in the worktree\n{listing}"
    body = result.output.strip()
    output = f"VERDICT: FAIL\n{finding}"
    if body:
        output = f"{output}\n\n{body}"
    return AgentResult(
        ok=False,
        output=output,
        error=result.error or finding,
        timed_out=result.timed_out,
        stalled=result.stalled,
        duration_seconds=result.duration_seconds,
        model=result.model,
        quota_exhausted=result.quota_exhausted,
        quota_scope=result.quota_scope,
        capacity_exhausted=result.capacity_exhausted,
        transient_error=result.transient_error,
        waiting_cause=result.waiting_cause,
        auth_failure=result.auth_failure,
        model_unavailable=result.model_unavailable,
        provider=result.provider,
        input_tokens=result.input_tokens,
        cached_input_tokens=result.cached_input_tokens,
        output_tokens=result.output_tokens,
        reasoning_output_tokens=result.reasoning_output_tokens,
    )


def cursor_mcp_auto_approve_argv(cursor_command: str) -> list[str]:
    try:
        proc = subprocess.run(
            prepare_external_argv([cursor_command, "--help"]),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    help_text = (proc.stdout or "") + (proc.stderr or "")
    if "--approve-mcps" in help_text:
        return ["--approve-mcps"]
    return []


def cursor_mcp_config_mentions_playwright(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    servers = raw.get("mcpServers") or raw.get("mcp_servers") or {}
    if not isinstance(servers, dict):
        return False
    for name, cfg in servers.items():
        if "playwright" in str(name).casefold():
            return True
        if not isinstance(cfg, dict):
            continue
        command = str(cfg.get("command") or "")
        if "playwright" in command.casefold():
            return True
        args = cfg.get("args") or []
        if isinstance(args, list) and any("playwright" in str(arg).casefold() for arg in args):
            return True
    return False


def port_in_use(port: int) -> bool:
    if port <= 0 or port > 65535:
        return False
    if os.name == "nt":
        script = (
            f"$c = Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | "
            "Select-Object -First 1; if ($c) { 'in-use' }"
        )
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return "in-use" in (proc.stdout or "")
    if _command_available("lsof"):
        try:
            proc = subprocess.run(
                ["lsof", "-i", f":{port}", "-t"],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=15,
            )
            return proc.returncode == 0 and bool((proc.stdout or "").strip())
        except (OSError, subprocess.TimeoutExpired):
            return False
    return False


def _command_available(command: str) -> bool:
    try:
        proc = subprocess.run(
            ["which", command] if os.name != "nt" else ["where", command],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _posix_parent_pid(pid: int) -> int | None:
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-o", "ppid="],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = (proc.stdout or "").split()
    if not token:
        return None
    parent_text = token[-1]
    if not parent_text.isdigit():
        return None
    return int(parent_text)


def _protected_process_ids() -> set[int]:
    protected: set[int] = set()
    if os.name == "nt":
        protected.add(os.getpid())
        script = f"""
$ids = New-Object System.Collections.Generic.HashSet[int]
$p = {os.getpid()}
for ($i = 0; $i -lt 64; $i++) {{
  [void]$ids.Add([int]$p)
  $proc = Get-CimInstance Win32_Process -Filter "ProcessId=$p" -ErrorAction SilentlyContinue
  if (-not $proc -or $proc.ParentProcessId -le 0) {{ break }}
  $p = $proc.ParentProcessId
}}
($ids | Sort-Object) -join ','
"""
        try:
            proc = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", script],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.TimeoutExpired):
            return protected
        for part in (proc.stdout or "").split(","):
            part = part.strip()
            if part.isdigit():
                protected.add(int(part))
        return protected

    to_visit = [os.getpid()]
    while to_visit and len(protected) < 64:
        pid = to_visit.pop()
        if pid <= 0 or pid in protected:
            continue
        protected.add(pid)
        parent = _posix_parent_pid(pid)
        if parent is None:
            break
        if parent > 0 and parent not in protected:
            to_visit.append(parent)
    if not protected:
        protected.add(os.getpid())
    return protected


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            proc = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return str(pid) in (proc.stdout or "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _collect_processes_for_ports(ports: list[int], protected: set[int]) -> dict[int, str]:
    targets: dict[int, str] = {}
    for port in ports:
        if port <= 0:
            continue
        if os.name == "nt":
            script = (
                f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue | "
                "ForEach-Object { $_.OwningProcess } | Sort-Object -Unique"
            )
            try:
                proc = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", script],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=20,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if line.isdigit():
                    pid = int(line)
                    if pid not in protected:
                        targets.setdefault(pid, f"listening on port {port}")
            continue
        if _command_available("lsof"):
            try:
                proc = subprocess.run(
                    ["lsof", "-i", f":{port}", "-t"],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=15,
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            for line in (proc.stdout or "").splitlines():
                line = line.strip()
                if line.isdigit():
                    pid = int(line)
                    if pid not in protected:
                        targets.setdefault(pid, f"listening on port {port}")
    return targets


def _normalize_command_path_text(text: str) -> str:
    normalized = text.replace("\\", "/")
    if os.name == "nt":
        normalized = normalized.lower()
    return normalized


def _continues_path_component(char: str) -> bool:
    """True when `char` can extend the final path component (sibling worktree id)."""
    return char.isalnum() or char in "._-"


def _command_contains_worktree_path(command: str, worktree: Path) -> bool:
    """True when `command` names this worktree, not a sibling with a prefix-matching id."""
    needle = _normalize_command_path_text(str(worktree.resolve()).rstrip("/\\"))
    if not command or not needle:
        return False
    haystack = _normalize_command_path_text(command)
    start = 0
    while True:
        index = haystack.find(needle, start)
        if index < 0:
            return False
        end = index + len(needle)
        if end >= len(haystack) or not _continues_path_component(haystack[end]):
            return True
        start = index + 1


def _collect_processes_for_worktree(
    worktree: Path,
    protected: set[int],
    lookup_failures: list[str] | None = None,
    agent_pid: int | None = None,
    agent_started_at: float | None = None,
) -> dict[int, str]:
    needle = str(worktree.resolve())
    targets: dict[int, str] = {}
    if os.name == "nt":
        script = (
            "$ErrorActionPreference = 'Stop'; Get-CimInstance Win32_Process | "
            "ForEach-Object { $created = if ($_.CreationDate) { "
            "$_.CreationDate.ToUniversalTime().ToString('o') } else { '' }; "
            "\"$($_.ProcessId)`t$($_.ParentProcessId)`t$created`t$($_.CommandLine)\" }"
        )
        proc: subprocess.CompletedProcess[str] | None = None
        failure = ""
        for _attempt in range(2):
            try:
                candidate = subprocess.run(
                    ["powershell.exe", "-NoProfile", "-Command", script],
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=90,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                failure = type(exc).__name__
                continue
            if candidate.returncode != 0:
                failure = f"exit code {candidate.returncode}"
                continue
            proc = candidate
            break
        if proc is not None and failure and lookup_failures is not None:
            lookup_failures.append(f"worktree process lookup failed: {failure}; retry succeeded")
        if proc is None:
            if lookup_failures is not None:
                lookup_failures.append(f"worktree process lookup failed after 2 attempts: {failure}")
            return targets

        processes: dict[int, tuple[int, float | None, str]] = {}
        for line in (proc.stdout or "").splitlines():
            parts = line.split("\t", 3)
            if len(parts) != 4:
                continue
            pid_text, parent_text, created_text, command = parts
            if not pid_text.strip().isdigit() or not parent_text.strip().isdigit():
                continue
            pid = int(pid_text.strip())
            parent_pid = int(parent_text.strip())
            command = command.strip()
            try:
                created_at = datetime.fromisoformat(created_text).timestamp()
            except (ValueError, OverflowError, OSError):
                created_at = None
            processes[pid] = (parent_pid, created_at, command)

        def descends_from_agent(pid: int) -> bool:
            if (
                agent_pid is None or agent_pid <= 1 or agent_pid == os.getpid()
                or agent_pid in protected or agent_started_at is None
            ):
                return False
            seen: set[int] = set()
            current = pid
            while current not in seen:
                seen.add(current)
                process = processes.get(current)
                if process is None:
                    return False
                parent_pid, created_at, _command = process
                if created_at is None or not created_at > agent_started_at:
                    return False
                if parent_pid == agent_pid:
                    return True
                if parent_pid <= 0:
                    return False
                current = parent_pid
            return False

        for pid, (_parent_pid, _created_at, command) in processes.items():
            if pid in protected:
                continue
            if "Get-CimInstance Win32_Process" in command or "Get-NetTCPConnection" in command:
                continue
            if not (
                _command_contains_worktree_path(command, worktree)
                or descends_from_agent(pid)
            ):
                continue
            targets[pid] = command or needle
        return targets

    stdout = _posix_ps("pid=,args=") or _posix_ps("pid=,command=")
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        pid_text, command = parts
        if not pid_text.isdigit():
            continue
        pid = int(pid_text)
        if pid in protected or not _command_contains_worktree_path(command, worktree):
            continue
        targets[pid] = command
    return targets


def _posix_ps(format_spec: str) -> str:
    try:
        proc = subprocess.run(
            ["ps", "-eo", format_spec],
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def _posix_descendant_pids(root_pid: int) -> list[int]:
    children: dict[int, list[int]] = {}
    for line in _posix_ps("pid=,ppid=").splitlines():
        parts = line.split()
        if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
            continue
        pid, ppid = int(parts[0]), int(parts[1])
        children.setdefault(ppid, []).append(pid)
    descendants: list[int] = []
    stack = [root_pid]
    seen = {root_pid}
    while stack:
        current = stack.pop()
        for child in children.get(current, []):
            if child in seen:
                continue
            seen.add(child)
            descendants.append(child)
            stack.append(child)
    descendants.reverse()
    return descendants


def _stop_discovered_pid(pid: int, protected: set[int]) -> None:
    """Stop a leftover review process without signaling the harness process group."""
    if pid <= 1 or pid in protected:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    victims = [child for child in _posix_descendant_pids(pid) if child > 1 and child not in protected]
    victims.append(pid)
    for target in victims:
        try:
            os.kill(target, signal.SIGTERM)
        except OSError:
            continue
        if _process_is_alive(target):
            try:
                os.kill(target, getattr(signal, "SIGKILL", signal.SIGTERM))
            except OSError:
                pass


def snapshot_port_listeners(ports: list[int]) -> dict[int, str]:
    return _collect_processes_for_ports(ports, _protected_process_ids())


def cleanup_ui_review_processes(
    *,
    config: UIReviewConfig,
    worktree: Path,
    log_dir: Path,
    task_id: str,
    ports_before: dict[int, str] | None = None,
    agent_pid: int | None = None,
    agent_started_at: float | None = None,
) -> None:
    """Clean up UI processes; agent_started_at is the call's Unix wall-clock time."""
    if not config.enabled:
        return
    protected = _protected_process_ids()
    targets: dict[int, str] = {}
    lookup_failures: list[str] = []
    current_ports = _collect_processes_for_ports(config.ports, protected)
    if ports_before is None:
        targets.update(current_ports)
    else:
        for pid, command in current_ports.items():
            if pid not in ports_before:
                targets[pid] = command
    targets.update(
        _collect_processes_for_worktree(
            worktree,
            protected,
            lookup_failures,
            agent_pid,
            agent_started_at,
        )
    )
    if not targets and not lookup_failures:
        return
    lines = list(lookup_failures)
    for pid, command in sorted(targets.items()):
        if not _process_is_alive(pid):
            continue
        _stop_discovered_pid(pid, protected)
        lines.append(f"stopped pid={pid} command={command}")
    if lines:
        write_log(log_dir, f"{task_id}-ui-review-cleanup.log", "\n".join(lines) + "\n")
