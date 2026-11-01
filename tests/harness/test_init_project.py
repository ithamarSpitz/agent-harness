from __future__ import annotations

import io
import subprocess
from contextlib import redirect_stdout
from pathlib import Path

from harness.cli import main
from harness.config import HarnessConfig
from harness.init_project import HARNESS_AGENTS_BLOCK, HARNESS_YAML


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def init_repo(tmp_path: Path) -> None:
    run(["git", "init", "-b", "main"], tmp_path)
    run(["git", "config", "user.name", "Test"], tmp_path)
    run(["git", "config", "user.email", "test@example.com"], tmp_path)


def run_init_capture(root: Path, *, force: bool = False) -> str:
    argv = ["--root", str(root), "init"]
    if force:
        argv.append("--force")
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    assert code == 0
    return buf.getvalue()


def test_init_creates_project_and_validates(tmp_path: Path):
    init_repo(tmp_path)
    run_init_capture(tmp_path)

    config = HarnessConfig.load(tmp_path / "harness.yaml")
    assert config.verification.global_task_commands == ["git diff --check"]
    assert config.retry.sequence == ["worker", "hard_worker", "hard_worker"]

    assert main(["--root", str(tmp_path), "validate"]) == 0


def test_init_second_run_skips_existing_files(tmp_path: Path):
    init_repo(tmp_path)
    run_init_capture(tmp_path)
    out = run_init_capture(tmp_path)
    for rel in (
        "harness.yaml",
        "tasks/roadmap.yaml",
        "tasks/TASK_TEMPLATE.md",
        "tasks/sprint-01-example/01-example.md",
        "AGENTS.md",
        ".gitignore",
    ):
        assert f"{rel}: skipped (exists)" in out


def test_init_appends_agents_and_gitignore_without_duplicates(tmp_path: Path):
    init_repo(tmp_path)
    write(tmp_path / "AGENTS.md", "# Custom\n\nKeep this.\n")
    write(tmp_path / ".gitignore", ".harness/\n")

    out = run_init_capture(tmp_path)

    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "Keep this." in agents
    assert HARNESS_AGENTS_BLOCK.strip() in agents

    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert gitignore.count(".harness/") == 1
    assert "graphify-out/" not in gitignore

    assert "AGENTS.md: updated" in out

    out2 = run_init_capture(tmp_path)
    assert "AGENTS.md: skipped (exists)" in out2
    assert ".gitignore: skipped (exists)" in out2


def test_init_force_rewrites_templates_not_agents_or_gitignore(tmp_path: Path):
    init_repo(tmp_path)
    run_init_capture(tmp_path)

    write(tmp_path / "harness.yaml", "execution: {}\n")
    write(tmp_path / "tasks/roadmap.yaml", "project: x\nsprints: {}\n")
    write(tmp_path / "tasks/TASK_TEMPLATE.md", "# broken\n")
    agents_before = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    gitignore_before = (tmp_path / ".gitignore").read_text(encoding="utf-8")

    run_init_capture(tmp_path, force=True)

    assert (tmp_path / "harness.yaml").read_text(encoding="utf-8") == HARNESS_YAML
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == agents_before
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == gitignore_before


def test_init_warns_when_not_git_repo(tmp_path: Path):
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(["--root", str(tmp_path), "init"])
    assert code == 0
    assert "WARNING: root is not a Git repository" in buf.getvalue()
    assert (tmp_path / "harness.yaml").exists()
