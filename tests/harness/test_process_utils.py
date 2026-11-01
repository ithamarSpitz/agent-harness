from __future__ import annotations

import sys

from harness.process_utils import prepare_external_argv, split_command


def test_windows_cmd_shim_is_routed_through_comspec(monkeypatch):
    monkeypatch.setenv("COMSPEC", r"C:\\Windows\\System32\\cmd.exe")
    argv = prepare_external_argv([r"C:\\Tools\\agent.cmd", "models"], windows=True)
    assert argv[:4] == [r"C:\\Windows\\System32\\cmd.exe", "/d", "/s", "/c"]
    assert "agent.cmd" in argv[4]
    assert "models" in argv[4]


def test_python_script_uses_current_interpreter_cross_platform(tmp_path):
    script = tmp_path / "fake-agent.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    argv = prepare_external_argv([str(script), "--version"], windows=True)
    assert argv == [sys.executable, str(script.resolve()), "--version"]


def test_simple_configured_command_splits_portably():
    assert split_command("docker compose") == ["docker", "compose"]


def test_windows_batch_cursor_uses_stdin_prompt_transport():
    from harness.process_utils import cursor_prompt_via_stdin

    assert cursor_prompt_via_stdin(r"C:\\Tools\\agent.cmd", windows=True)
    assert cursor_prompt_via_stdin(r"C:\\Tools\\cursor-agent.BAT", windows=True)
    assert not cursor_prompt_via_stdin(r"C:\\Tools\\agent.exe", windows=True)
    assert not cursor_prompt_via_stdin("agent", windows=False)


def test_stop_process_tree_also_stops_children(tmp_path):
    import os
    import subprocess
    import sys
    import time

    from harness.process_utils import stop_process_tree

    pid_file = tmp_path / "child.pid"
    parent_code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", parent_code],
        text=True,
        start_new_session=(os.name != "nt"),
        creationflags=(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0),
    )
    deadline = time.time() + 20
    while not (pid_file.exists() and pid_file.read_text().strip()) and time.time() < deadline:
        time.sleep(0.1)
    child_pid = int(pid_file.read_text())

    stop_process_tree(proc)

    assert proc.poll() is not None
    deadline = time.time() + 10
    while _pid_alive(child_pid) and time.time() < deadline:
        time.sleep(0.2)
    assert not _pid_alive(child_pid)


def _pid_alive(pid: int) -> bool:
    import os
    import subprocess

    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
