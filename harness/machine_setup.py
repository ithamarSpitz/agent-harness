"""Install the user-level files and tools needed by the harness."""
from __future__ import annotations

import json
import ntpath
import os
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
from collections.abc import Callable, Sequence
from pathlib import Path, PureWindowsPath

GRAPHIFY_VERSION = "0.9.100"
PLAYWRIGHT_SERVER = {
    "command": "npx",
    "args": ["-y", "@playwright/mcp@0.0.84"],
}
STATUSLINE_COMMAND = "python -P -m harness.statusline"
POSIX_PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'
WINDOWS_SET_PATH_SCRIPT = (
    '$value = [Console]::In.ReadToEnd(); '
    '[Environment]::SetEnvironmentVariable("Path", $value, "User")'
)

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
CommandFinder = Callable[[str], str | None]


def home_dir() -> Path:
    """Return the home directory used for user-level setup."""
    return Path.home()


def _run(
    command: Sequence[str],
    *,
    runner: CommandRunner,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            list(command),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            input=input_text,
        )
    except OSError as exc:
        return subprocess.CompletedProcess(list(command), 127, "", str(exc))


def _report(name: str, status: str) -> None:
    print(f"{name}: {status}", flush=True)


def _check_tools(*, finder: CommandFinder, runner: CommandRunner) -> None:
    tools = (
        ("git", "install Git"),
        ("node", "install Node.js"),
        ("npx", "install Node.js (includes npx)"),
        ("gh", "install GitHub CLI"),
        ("codex", "install Codex CLI"),
        ("agent", "install Cursor Agent CLI"),
    )
    _report("tool Python", "ok" if sys.version_info >= (3, 11) else "skipped (requires Python 3.11 or newer)")
    found: dict[str, str | None] = {}
    for tool, hint in tools:
        found[tool] = finder(tool)
        status = "ok" if found[tool] else f"skipped (missing; {hint})"
        _report(f"tool {tool}", status)

    login_checks = (
        ("gh", ["auth", "status"], "run gh auth login"),
        ("codex", ["login", "status"], "run codex login"),
        ("agent", ["status"], "log in with agent"),
    )
    for tool, arguments, hint in login_checks:
        executable = found[tool]
        if executable is None:
            _report(f"{tool} login", "skipped (tool missing)")
            continue
        result = _run([executable, *arguments], runner=runner)
        status = "ok" if result.returncode == 0 else f"skipped (not logged in; {hint})"
        _report(f"{tool} login", status)


def _user_scripts_dir() -> Path:
    return Path(sysconfig.get_path("scripts", scheme=sysconfig.get_preferred_scheme("user")))


def _install_graphify(*, runner: CommandRunner, platform: str) -> bool:
    probe = _run(["graphify", "--help"], runner=runner)
    if probe.returncode != 0:
        # A fresh shell may not yet include pip's user scripts directory.
        executable = _user_scripts_dir() / ("graphify.exe" if platform == "nt" else "graphify")
        probe = _run([str(executable), "--help"], runner=runner)
    if probe.returncode == 0:
        _report("graphify", "ok")
        return True
    install = _run(
        [sys.executable, "-m", "pip", "install", "--user", f"graphifyy=={GRAPHIFY_VERSION}"],
        runner=runner,
    )
    if install.returncode == 0:
        _report("graphify", "created")
        return True
    detail = (install.stderr or install.stdout).strip().splitlines()
    reason = detail[-1] if detail else f"pip exited {install.returncode}"
    _report("graphify", f"skipped (install failed: {reason})")
    return False


def _ensure_file(path: Path, content: str, *, executable: bool = False) -> str:
    existed = path.exists()
    existing_content = None
    if existed:
        with path.open("r", encoding="utf-8", newline="") as stream:
            existing_content = stream.read()
    same_content = existing_content == content
    mode_ok = os.name == "nt" or not executable or not existed or bool(path.stat().st_mode & 0o111)
    if same_content and mode_ok:
        return "ok"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not same_content:
        path.write_text(content, encoding="utf-8", newline="\n")
    if executable and os.name != "nt":
        path.chmod(path.stat().st_mode | 0o755)
    return "updated" if existed else "created"


def _setup_launchers(home: Path) -> Path:
    bin_dir = home / ".local" / "bin"
    launchers = {
        "harness.cmd": "@echo off\r\npython -P -m harness %*\r\n",
        "usage.cmd": "@echo off\r\npython -P -m harness limits %*\r\n",
        "harness": '#!/bin/sh\nexec python -P -m harness "$@"\n',
        "usage": '#!/bin/sh\nexec python -P -m harness limits "$@"\n',
    }
    for name, content in launchers.items():
        status = _ensure_file(bin_dir / name, content, executable=not name.endswith(".cmd"))
        _report(f"launcher {name}", status)
    return bin_dir


def _setup_windows_path(bin_dir: Path, *, runner: CommandRunner) -> bool:
    read = _run(
        [
            "powershell",
            "-NoProfile",
            "-Command",
            '[Environment]::GetEnvironmentVariable("Path", "User")',
        ],
        runner=runner,
    )
    if read.returncode != 0:
        _report("user PATH", "skipped (could not read Windows user PATH)")
        return False
    entries = [entry.strip() for entry in read.stdout.split(";") if entry.strip()]
    normalized_bin_dir = ntpath.normcase(ntpath.normpath(str(bin_dir)))
    if any(ntpath.normcase(ntpath.normpath(entry)) == normalized_bin_dir for entry in entries):
        _report("user PATH", "ok")
        return True
    new_path = ";".join([*entries, str(bin_dir)])
    write = _run(
        ["powershell", "-NoProfile", "-Command", WINDOWS_SET_PATH_SCRIPT],
        runner=runner,
        input_text=new_path,
    )
    if write.returncode != 0:
        _report("user PATH", "skipped (could not update Windows user PATH)")
        return False
    _report("user PATH", "updated")
    return True


def _setup_posix_path(home: Path) -> bool:
    profile = home / ".profile"
    existing = profile.read_text(encoding="utf-8") if profile.exists() else ""
    if POSIX_PATH_LINE in existing.splitlines():
        _report("user PATH", "ok")
        return True
    separator = "" if not existing or existing.endswith("\n") else "\n"
    profile.parent.mkdir(parents=True, exist_ok=True)
    profile.write_text(f"{existing}{separator}{POSIX_PATH_LINE}\n", encoding="utf-8", newline="\n")
    _report("user PATH", "updated" if existing else "created")
    return True


def _read_json_object(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("JSON root is not an object")
    return payload


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8", newline="\n")


def _is_legacy_statusline(command: object) -> bool:
    if not isinstance(command, str):
        return False
    try:
        # Preserve Windows backslashes while accepting quoted paths on either OS.
        parts = [part.strip('\"\'') for part in shlex.split(command, posix=False)]
    except ValueError:
        return False
    if len(parts) != 2:
        return False
    interpreter = PureWindowsPath(parts[0]).name
    return (
        re.fullmatch(r"(?:python(?:w)?(?:\d+(?:\.\d+)*)?|py)(?:\.exe)?", interpreter, re.IGNORECASE) is not None
        and PureWindowsPath(parts[1]).name == "statusline-usage.py"
    )


def _setup_claude(home: Path) -> bool:
    path = home / ".claude" / "settings.json"
    existed = path.exists()
    try:
        settings = _read_json_object(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _report("Claude status line", f"skipped (invalid settings.json: {exc})")
        return False
    current = settings.get("statusLine")
    if isinstance(current, dict) and current.get("command") == STATUSLINE_COMMAND:
        _report("Claude status line", "ok")
        return True
    command = current.get("command") if isinstance(current, dict) else None
    if current is not None and not _is_legacy_statusline(command):
        _report("Claude status line", "skipped (custom status line)")
        return True
    settings["statusLine"] = {"type": "command", "command": STATUSLINE_COMMAND}
    _write_json(path, settings)
    _report("Claude status line", "updated" if existed else "created")
    return True


def _setup_cursor(home: Path) -> bool:
    path = home / ".cursor" / "mcp.json"
    existed = path.exists()
    try:
        settings = _read_json_object(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _report("Cursor Playwright MCP", f"skipped (invalid mcp.json: {exc})")
        return False
    servers = settings.get("mcpServers")
    if servers is None:
        servers = {}
        settings["mcpServers"] = servers
    if not isinstance(servers, dict):
        _report("Cursor Playwright MCP", "skipped (mcpServers is not an object)")
        return False
    if "playwright" in servers:
        _report("Cursor Playwright MCP", "ok")
        return True
    servers["playwright"] = PLAYWRIGHT_SERVER
    _write_json(path, settings)
    _report("Cursor Playwright MCP", "updated" if existed else "created")
    return True


def run_setup_machine(
    *,
    home: Path | None = None,
    platform: str | None = None,
    finder: CommandFinder | None = None,
    runner: CommandRunner | None = None,
) -> int:
    """Configure one machine without installing system-level prerequisites."""
    target_home = home or home_dir()
    platform = platform or os.name
    finder = finder or shutil.which
    runner = runner or subprocess.run
    _check_tools(finder=finder, runner=runner)
    successful = _install_graphify(runner=runner, platform=platform)
    bin_dir = _setup_launchers(target_home)
    if platform == "nt":
        successful = _setup_windows_path(bin_dir, runner=runner) and successful
    else:
        successful = _setup_posix_path(target_home) and successful
    successful = _setup_claude(target_home) and successful
    successful = _setup_cursor(target_home) and successful
    return 0 if successful else 1
