from __future__ import annotations

import os
import signal
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable, Sequence


def split_command(command: str) -> list[str]:
    """Split a configured command line on the current platform.

    Configured commands are normally simple (for example ``agent`` or
    ``docker compose``). On Windows, ``shlex`` in POSIX mode corrupts
    backslashes, so use non-POSIX parsing and strip only wrapping quotes.
    """
    if not command.strip():
        return []
    if os.name != "nt":
        return shlex.split(command)
    parts = shlex.split(command, posix=False)
    cleaned: list[str] = []
    for part in parts:
        if len(part) >= 2 and part[0] == part[-1] and part[0] in {'"', "'"}:
            part = part[1:-1]
        cleaned.append(part)
    return cleaned


def locate_executable(command: str) -> str | None:
    """Resolve an executable token, also accepting an explicit script path."""
    candidate = Path(command)
    if candidate.is_file():
        return str(candidate.resolve())
    return shutil.which(command)



def locate_codex_executable(
    command: str,
    *,
    windows: bool | None = None,
    localappdata: str | None = None,
    which: Callable[[str], str | None] | None = None,
) -> str | None:
    """Resolve the Codex CLI, including the standard OpenAI Windows install path.

    A PowerShell process may have a stale PATH when Codex was installed after the
    shell started. In that case the PATH resolver fails even though the desktop
    installer placed ``codex.exe`` under LocalAppData. ``which`` is injectable
    so tests can model a stale PATH without being contaminated by a real Codex
    installation on the machine running the test suite.
    """
    candidate = Path(command)
    if candidate.is_file():
        return str(candidate.resolve())
    resolver = shutil.which if which is None else which
    resolved = resolver(command)
    if resolved:
        return resolved
    is_windows = os.name == "nt" if windows is None else windows
    if not is_windows:
        return None
    name = Path(command).name.casefold()
    if name not in {"codex", "codex.exe"}:
        return None
    base = localappdata if localappdata is not None else os.environ.get("LOCALAPPDATA", "")
    if not base:
        return None
    candidate = Path(base) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    return str(candidate.resolve()) if candidate.is_file() else None


def resolve_codex_argv(command: str, *args: str) -> list[str]:
    """Resolve a configured Codex command and append invocation arguments."""
    parts = split_command(command)
    if not parts:
        return []
    executable = locate_codex_executable(parts[0]) or parts[0]
    return [executable, *parts[1:], *args]


def cursor_prompt_via_stdin(command: str, *, windows: bool | None = None) -> bool:
    """Use stdin for Cursor prompts when the Windows launcher is a batch shim.

    The official Windows Cursor CLI currently resolves to ``agent.cmd``. Batch
    launchers route arguments through ``cmd.exe``/PowerShell before Node, which
    can corrupt long or quoted prompts and can also cause trailing flags such as
    ``--trust`` to disappear. Keep only short control flags in argv and stream the
    prompt over stdin for ``.cmd``/``.bat`` launchers.
    """
    is_windows = os.name == "nt" if windows is None else windows
    if not is_windows:
        return False
    resolved = locate_executable(command) or command
    return Path(resolved).suffix.lower() in {".cmd", ".bat"}

def prepare_external_argv(argv: Sequence[str], *, windows: bool | None = None) -> list[str]:
    """Return argv that Python can execute directly on this platform.

    Windows ``subprocess`` cannot execute ``.cmd``/``.bat`` shims directly in
    every context. Cursor's ``agent`` command can be installed as such a shim,
    so route it through ``cmd.exe``. Python scripts are routed through the
    current interpreter, which also makes harness test doubles portable.
    """
    if not argv:
        raise ValueError("external command is empty")

    is_windows = os.name == "nt" if windows is None else windows
    resolved = locate_executable(str(argv[0])) or str(argv[0])
    rest = [str(part) for part in argv[1:]]
    suffix = Path(resolved).suffix.lower()

    if suffix == ".py":
        return [sys.executable, resolved, *rest]

    if is_windows and suffix in {".cmd", ".bat"}:
        comspec = os.environ.get("COMSPEC") or "cmd.exe"
        command_line = subprocess.list2cmdline([resolved, *rest])
        return [comspec, "/d", "/s", "/c", command_line]

    if is_windows and suffix == ".ps1":
        powershell = shutil.which("powershell.exe") or shutil.which("powershell") or "powershell.exe"
        return [
            powershell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            resolved,
            *rest,
        ]

    return [resolved, *rest]


def stop_process_tree(proc: subprocess.Popen[str]) -> None:
    """Stop an agent CLI and every child it spawned.

    Agent CLIs launch shells, Node processes and other tools (on Windows behind a .cmd or
    PowerShell shim). A timeout or stall must not leave those processes running after the
    provider call is cancelled.
    """
    if proc.poll() is not None:
        return
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                text=True, encoding="utf-8", errors="replace",
                capture_output=True, timeout=10, check=False,
            )
            proc.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        killpg = getattr(os, "killpg", None)
        getpgid = getattr(os, "getpgid", None)
        sigkill = getattr(signal, "SIGKILL", None)
        if killpg is not None and getpgid is not None:
            try:
                killpg(getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=5)
                return
            except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
                pass
            if sigkill is not None:
                try:
                    killpg(getpgid(proc.pid), sigkill)
                    return
                except (OSError, ProcessLookupError):
                    pass
    try:
        proc.kill()
    except OSError:
        pass
