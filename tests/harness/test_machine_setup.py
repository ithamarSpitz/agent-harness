from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from harness.cli import main
from harness.machine_setup import (
    PLAYWRIGHT_SERVER,
    STATUSLINE_COMMAND,
    WINDOWS_SET_PATH_SCRIPT,
    run_setup_machine,
)


@pytest.fixture(autouse=True)
def fake_user_scripts(monkeypatch, tmp_path):
    monkeypatch.setattr("harness.machine_setup._user_scripts_dir", lambda: tmp_path / "Python" / "Scripts")


class FakeCommands:
    def __init__(self, *, available: set[str] | None = None, logged_in: bool = True):
        self.available = available if available is not None else {"git", "node", "npx", "gh", "codex", "agent"}
        self.logged_in = logged_in
        self.calls: list[list[str]] = []

    def which(self, command: str) -> str | None:
        return f"/fake/{command}" if command in self.available else None

    def run(self, command, **kwargs):
        del kwargs
        command = list(command)
        self.calls.append(command)
        if command == ["graphify", "--help"]:
            return subprocess.CompletedProcess(command, 0, "help", "")
        normalized = [Path(command[0]).name, *command[1:]]
        if normalized in (["gh", "auth", "status"], ["codex", "login", "status"], ["agent", "status"]):
            return subprocess.CompletedProcess(command, 0 if self.logged_in else 1, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")


def _run(home: Path, fake: FakeCommands) -> int:
    return run_setup_machine(home=home, platform="posix", finder=fake.which, runner=fake.run)


def test_empty_home_is_created_and_second_run_is_idempotent(tmp_path, capsys):
    fake = FakeCommands()

    assert _run(tmp_path, fake) == 0
    first_contents = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    first_output = capsys.readouterr().out
    assert "created" in first_output
    assert json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))["statusLine"] == {
        "type": "command",
        "command": STATUSLINE_COMMAND,
    }
    assert json.loads((tmp_path / ".cursor" / "mcp.json").read_text(encoding="utf-8"))["mcpServers"][
        "playwright"
    ] == PLAYWRIGHT_SERVER
    for launcher in ("harness.cmd", "harness", "usage.cmd", "usage"):
        assert (tmp_path / ".local" / "bin" / launcher).is_file()

    assert _run(tmp_path, fake) == 0
    second_contents = {path.relative_to(tmp_path): path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    second_output = capsys.readouterr().out
    assert first_contents == second_contents
    for launcher in ("harness.cmd", "harness", "usage.cmd", "usage"):
        assert f"launcher {launcher}: ok" in second_output
    assert "Claude status line: ok" in second_output
    assert "Cursor Playwright MCP: ok" in second_output
    assert "user PATH: ok" in second_output


@pytest.mark.parametrize("command", [
    "my-status", "python custom-statusline-usage.py", "echo statusline-usage.py",
    "python /old/statusline-usage.py.backup", "python other.py statusline-usage.py",
    'python -c "print(\'statusline-usage.py\')"',
])
def test_existing_settings_survive_and_custom_statusline_is_preserved(tmp_path, command):
    claude_path = tmp_path / ".claude" / "settings.json"
    claude_path.parent.mkdir(parents=True)
    custom = {"theme": "dark", "statusLine": {"type": "command", "command": command}}
    claude_path.write_text(json.dumps(custom), encoding="utf-8")
    cursor_path = tmp_path / ".cursor" / "mcp.json"
    cursor_path.parent.mkdir(parents=True)
    cursor_path.write_text(
        json.dumps({"other": 1, "mcpServers": {"custom": {"command": "server"}}}), encoding="utf-8"
    )

    assert _run(tmp_path, FakeCommands()) == 0

    assert json.loads(claude_path.read_text(encoding="utf-8")) == custom
    cursor = json.loads(cursor_path.read_text(encoding="utf-8"))
    assert cursor["other"] == 1
    assert cursor["mcpServers"]["custom"] == {"command": "server"}
    assert cursor["mcpServers"]["playwright"] == PLAYWRIGHT_SERVER


@pytest.mark.parametrize("command", [
    "python /old/statusline-usage.py",
    'python "C:\\Users\\Some User\\.claude\\statusline-usage.py"',
    '"C:\\Program Files\\Python\\python.exe" "C:\\Users\\Some User\\statusline-usage.py"',
    "python3 '/home/some user/.claude/statusline-usage.py'",
])
def test_legacy_statusline_is_replaced(tmp_path, command):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"keep": True, "statusLine": {"type": "command", "command": command}}),
        encoding="utf-8",
    )

    assert _run(tmp_path, FakeCommands()) == 0

    settings = json.loads(path.read_text(encoding="utf-8"))
    assert settings["keep"] is True
    assert settings["statusLine"] == {"type": "command", "command": STATUSLINE_COMMAND}


def test_missing_tools_and_logins_are_reported_but_do_not_fail(tmp_path, capsys):
    fake = FakeCommands(available={"git", "gh", "codex", "agent"}, logged_in=False)

    assert _run(tmp_path, fake) == 0

    output = capsys.readouterr().out
    assert "tool node: skipped (missing; install Node.js)" in output
    assert "tool npx: skipped (missing; install Node.js (includes npx))" in output
    assert "gh login: skipped (not logged in; run gh auth login)" in output
    assert "codex login: skipped (not logged in; run codex login)" in output
    assert "agent login: skipped (not logged in; log in with agent)" in output


def test_login_checks_use_executable_paths_returned_by_finder(tmp_path):
    fake = FakeCommands()
    executable_paths = {
        "gh": r"C:\Program Files\GitHub CLI\gh.exe",
        "codex": r"C:\Users\test\AppData\Roaming\npm\codex.CMD",
        "agent": r"C:\Users\test\AppData\Local\Programs\cursor\agent.CMD",
    }

    def finder(command):
        return executable_paths.get(command, f"C:\\Tools\\{command}.exe")

    assert run_setup_machine(
        home=tmp_path, platform="posix", finder=finder, runner=fake.run
    ) == 0

    assert [executable_paths["gh"], "auth", "status"] in fake.calls
    assert [executable_paths["codex"], "login", "status"] in fake.calls
    assert [executable_paths["agent"], "status"] in fake.calls


def test_graphify_is_installed_only_when_probe_fails(tmp_path):
    fake = FakeCommands()

    def fail_probe(command, **kwargs):
        command = list(command)
        fake.calls.append(command)
        return subprocess.CompletedProcess(command, 1 if command[-1] == "--help" else 0, "", "")

    assert run_setup_machine(
        home=tmp_path, platform="posix", finder=fake.which, runner=fail_probe
    ) == 0
    assert any(command[1:4] == ["-m", "pip", "install"] for command in fake.calls)


@pytest.mark.parametrize("platform", ["nt", "posix"])
def test_graphify_installed_in_user_scripts_is_found_on_second_run(tmp_path, capsys, platform):
    fake = FakeCommands()
    installed = False
    user_path = ""
    executable = tmp_path / "Python" / "Scripts" / ("graphify.exe" if platform == "nt" else "graphify")

    def runner(command, **kwargs):
        nonlocal installed, user_path
        command = list(command)
        fake.calls.append(command)
        if command == ["graphify", "--help"]:
            raise FileNotFoundError("user scripts are not on this shell's PATH")
        if command == [str(executable), "--help"]:
            return subprocess.CompletedProcess(command, 0 if installed else 1, "", "")
        if command[1:4] == ["-m", "pip", "install"]:
            installed = True
        if "GetEnvironmentVariable" in " ".join(command):
            return subprocess.CompletedProcess(command, 0, user_path, "")
        if "SetEnvironmentVariable" in " ".join(command):
            user_path = kwargs["input"]
        return subprocess.CompletedProcess(command, 0, "", "")

    assert run_setup_machine(home=tmp_path, platform=platform, finder=fake.which, runner=runner) == 0
    assert "graphify: created" in capsys.readouterr().out
    first_contents = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    assert run_setup_machine(home=tmp_path, platform=platform, finder=fake.which, runner=runner) == 0
    assert "graphify: ok" in capsys.readouterr().out
    assert sum(command[1:4] == ["-m", "pip", "install"] for command in fake.calls) == 1
    assert first_contents == {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}


def test_windows_user_path_is_read_and_updated_without_touching_process_path(tmp_path):
    fake = FakeCommands()
    write_kwargs = {}

    def windows_runner(command, **kwargs):
        command = list(command)
        fake.calls.append(command)
        if command == ["graphify", "--help"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if "GetEnvironmentVariable" in " ".join(command):
            return subprocess.CompletedProcess(command, 0, r"C:\Existing", "")
        if "SetEnvironmentVariable" in " ".join(command):
            write_kwargs.update(kwargs)
        return subprocess.CompletedProcess(command, 0, "", "")

    assert run_setup_machine(
        home=tmp_path, platform="nt", finder=fake.which, runner=windows_runner
    ) == 0

    path_writes = [command for command in fake.calls if "SetEnvironmentVariable" in " ".join(command)]
    assert path_writes == [["powershell", "-NoProfile", "-Command", WINDOWS_SET_PATH_SCRIPT]]
    assert write_kwargs["input"] == rf"C:\Existing;{tmp_path / '.local' / 'bin'}"


@pytest.mark.skipif(shutil.which("powershell") is None, reason="PowerShell is not installed")
def test_windows_path_update_script_parses_in_powershell():
    parser_script = (
        "$tokens = $null; $errors = $null; "
        "[System.Management.Automation.Language.Parser]::ParseInput("
        "[Console]::In.ReadToEnd(), [ref]$tokens, [ref]$errors) > $null; "
        "if ($errors.Count) { exit 1 }"
    )

    result = subprocess.run(
        ["powershell", "-NoProfile", "-Command", parser_script],
        input=WINDOWS_SET_PATH_SCRIPT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_cli_setup_machine_does_not_build_project_runtime(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr("harness.cli.run_setup_machine", lambda: called.append(True) or 0)

    assert main(["--root", str(tmp_path), "setup-machine"]) == 0
    assert called == [True]
