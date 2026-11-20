from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path

from harness.cli import build_runtime, cmd_doctor
from harness.adapters.base import FindResult
from harness.kiro_usage import KiroQuota
from tests.portable import make_python_script, yaml_quote


def _run(command: list[str], cwd: Path) -> None:
    subprocess.run(command, cwd=cwd, check=True, capture_output=True, text=True)


def _project(tmp_path: Path, *, signed_in: bool, enabled: bool = True) -> Path:
    _run(["git", "init", "-b", "main"], tmp_path)
    _run(["git", "config", "user.name", "Test"], tmp_path)
    _run(["git", "config", "user.email", "test@example.com"], tmp_path)
    fake = make_python_script(
        tmp_path / "fake-kiro.py",
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('kiro-cli 3.1.0')\n"
        "elif 'whoami' in sys.argv:\n"
        f"    print('Logged in as secret@example.com'); sys.exit({0 if signed_in else 1})\n",
    )
    (tmp_path / ".gitignore").write_text(".harness/\n", encoding="utf-8")
    (tmp_path / "harness.yaml").write_text(
        "codex:\n  enabled: false\n"
        "cursor:\n  enabled: false\n"
        f"kiro:\n  enabled: {str(enabled).lower()}\n"
        f"  command: {yaml_quote(fake)}\n",
        encoding="utf-8",
    )
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "A.md").write_text(
        "---\nid: A\nmodel_class: worker\nallowed_paths: [out.txt]\n"
        "verification: [echo ok]\n---\n# A\n",
        encoding="utf-8",
    )
    (tasks / "roadmap.yaml").write_text(
        "project: test\nsprints:\n  s1:\n    tasks:\n"
        "      A: {file: tasks/A.md, stage: 1, depends_on: []}\n",
        encoding="utf-8",
    )
    return fake


def _runtime(tmp_path: Path):
    return build_runtime(
        tmp_path,
        tmp_path / "tasks" / "roadmap.yaml",
        tmp_path / "harness.yaml",
    )


def test_doctor_reports_kiro_version_login_and_quota(tmp_path, capsys, monkeypatch):
    _project(tmp_path, signed_in=True)
    monkeypatch.setattr(
        "harness.adapters.kiro.KiroAdapter.find",
        lambda self, *, refresh=False: FindResult(
            True, "kiro-cli 3.1.0", True, "signed in"
        ),
    )
    monkeypatch.setattr(
        "harness.cli.fetch_kiro_usage",
        lambda *, command: KiroQuota(
            "KIRO FREE",
            0.2,
            50,
            0.4,
            datetime(2026, 12, 1, tzinfo=timezone.utc),
        ),
    )

    assert cmd_doctor(_runtime(tmp_path), full=False) == 0
    output = capsys.readouterr().out
    assert "kiro-cli/version" in output
    assert "kiro-cli 3.1.0" in output
    assert "kiro-cli/login" in output
    assert "signed in" in output
    assert "kiro-quota" in output
    assert "49.80 of 50 credits left, resets Tue Dec 01, 2026" in output
    assert "secret@example.com" not in output


def test_doctor_reports_kiro_not_signed_in(tmp_path, capsys, monkeypatch):
    _project(tmp_path, signed_in=False)
    monkeypatch.setattr(
        "harness.adapters.kiro.KiroAdapter.find",
        lambda self, *, refresh=False: FindResult(
            True, "kiro-cli 3.1.0", False, "not signed in"
        ),
    )
    monkeypatch.setattr(
        "harness.cli.fetch_kiro_usage",
        lambda *, command: KiroQuota(
            "KIRO FREE", 0, 50, 0, datetime(2026, 12, 1, tzinfo=timezone.utc)
        ),
    )

    assert cmd_doctor(_runtime(tmp_path), full=False) == 1
    output = capsys.readouterr().out
    assert "kiro-cli/login" in output
    assert "not signed in: run kiro-cli login" in output
    assert "secret@example.com" not in output


def test_doctor_omits_kiro_checks_when_disabled(tmp_path, capsys):
    _project(tmp_path, signed_in=True, enabled=False)

    assert cmd_doctor(_runtime(tmp_path), full=False) == 0
    output = capsys.readouterr().out
    assert "kiro-cli/version" not in output
    assert "kiro-cli/login" not in output
    assert "kiro-quota" not in output
