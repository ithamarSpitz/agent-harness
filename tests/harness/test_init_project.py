from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from pathlib import Path

from harness.cli import main
from harness.config import HarnessConfig
from harness.init_project import ARCHITECTURE_MD, HARNESS_AGENTS_BLOCK, HARNESS_YAML


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def init_repo(tmp_path: Path) -> None:
    run(["git", "init", "-b", "main"], tmp_path)
    run(["git", "config", "user.name", "Test"], tmp_path)
    run(["git", "config", "user.email", "test@example.com"], tmp_path)


def run_init_capture(
    root: Path,
    *,
    force: bool = False,
    public: bool = False,
    no_github: bool = False,
) -> str:
    argv = ["--root", str(root), "init"]
    if force:
        argv.append("--force")
    if public:
        argv.append("--public")
    if no_github:
        argv.append("--no-github")
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = main(argv)
    assert code == 0
    return buf.getvalue()


def install_fake_gh(
    tmp_path: Path,
    monkeypatch,
    *,
    logged_in: bool = True,
) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    run(["git", "init", "--bare", str(remote)], tmp_path)
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir()
    calls_path = tmp_path / "gh-calls.jsonl"
    script = fake_bin / ("fake_gh.py" if os.name == "nt" else "gh")
    script.write_text(
        """#!/usr/bin/env python3
import json
import os
import subprocess
import sys

args = sys.argv[1:]
with open(os.environ["FAKE_GH_CALLS"], "a", encoding="utf-8") as stream:
    stream.write(json.dumps(args) + "\\n")
if args == ["auth", "status"]:
    raise SystemExit(0 if os.environ["FAKE_GH_LOGGED_IN"] == "1" else 1)
if args[:2] == ["repo", "create"]:
    subprocess.run(
        ["git", "remote", "add", "origin", os.environ["FAKE_GH_REMOTE"]],
        check=True,
    )
    raise SystemExit(0)
raise SystemExit(2)
""",
        encoding="utf-8",
    )
    if os.name == "nt":
        write(fake_bin / "gh.cmd", f'@"{sys.executable}" "{script}" %*\n')
    else:
        script.chmod(0o755)
    monkeypatch.setenv("FAKE_GH_CALLS", str(calls_path))
    monkeypatch.setenv("FAKE_GH_LOGGED_IN", "1" if logged_in else "0")
    monkeypatch.setenv("FAKE_GH_REMOTE", str(remote))
    monkeypatch.setenv("PATH", str(fake_bin) + os.pathsep + os.environ["PATH"])
    return remote, calls_path


def read_gh_calls(calls_path: Path) -> list[list[str]]:
    if not calls_path.exists():
        return []
    return [json.loads(line) for line in calls_path.read_text(encoding="utf-8").splitlines()]


def isolate_missing_git_identity(tmp_path: Path, monkeypatch) -> None:
    """Hide ambient Git identity so init must supply its own committer."""
    for key in (
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
        "EMAIL",
    ):
        monkeypatch.delenv(key, raising=False)
    config = tmp_path / "isolated.gitconfig"
    config.write_text("[user]\n\tuseConfigOnly = true\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))


def test_init_creates_project_and_validates(tmp_path: Path):
    init_repo(tmp_path)
    run_init_capture(tmp_path, no_github=True)

    config = HarnessConfig.load(tmp_path / "harness.yaml")
    assert config.verification.global_task_commands == ["git diff --check"]
    assert config.retry.sequence == ["worker", "hard_worker", "hard_worker"]
    assert "ARCHITECTURE.md" in config.paths.protected
    assert config.context.aliases["architecture"] == "ARCHITECTURE.md"
    assert (tmp_path / "ARCHITECTURE.md").read_text(encoding="utf-8") == ARCHITECTURE_MD

    assert main(["--root", str(tmp_path), "validate"]) == 0


def test_init_second_run_skips_existing_files(tmp_path: Path):
    init_repo(tmp_path)
    run_init_capture(tmp_path, no_github=True)
    out = run_init_capture(tmp_path, no_github=True)
    for rel in (
        "harness.yaml",
        "ARCHITECTURE.md",
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

    out = run_init_capture(tmp_path, no_github=True)

    agents = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "Keep this." in agents
    assert HARNESS_AGENTS_BLOCK.strip() in agents

    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert gitignore.count(".harness/") == 1
    assert "graphify-out/" not in gitignore

    assert "AGENTS.md: updated" in out

    out2 = run_init_capture(tmp_path, no_github=True)
    assert "AGENTS.md: skipped (exists)" in out2
    assert ".gitignore: skipped (exists)" in out2


def test_init_force_rewrites_templates_not_agents_or_gitignore(tmp_path: Path):
    init_repo(tmp_path)
    run_init_capture(tmp_path, no_github=True)

    write(tmp_path / "harness.yaml", "execution: {}\n")
    write(tmp_path / "tasks/roadmap.yaml", "project: x\nsprints: {}\n")
    write(tmp_path / "tasks/TASK_TEMPLATE.md", "# broken\n")
    agents_before = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    gitignore_before = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    write(tmp_path / "ARCHITECTURE.md", "# Custom architecture\n")

    run_init_capture(tmp_path, force=True, no_github=True)

    assert (tmp_path / "harness.yaml").read_text(encoding="utf-8") == HARNESS_YAML
    assert (tmp_path / "AGENTS.md").read_text(encoding="utf-8") == agents_before
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == gitignore_before
    assert (tmp_path / "ARCHITECTURE.md").read_text(encoding="utf-8") == "# Custom architecture\n"


def test_init_creates_repo_github_commit_and_push(tmp_path: Path, monkeypatch):
    project = tmp_path / "my-project"
    project.mkdir()
    remote, calls_path = install_fake_gh(tmp_path, monkeypatch)
    isolate_missing_git_identity(tmp_path, monkeypatch)

    out = run_init_capture(project)

    assert run(["git", "branch", "--show-current"], project) == "main"
    assert run(["git", "log", "-1", "--format=%s"], project) == "Initial harness setup"
    assert run(["git", "log", "-1", "--format=%an <%ae>"], project) == "Agent Harness <harness@local>"
    assert run(["git", "remote", "get-url", "origin"], project) == str(remote)
    assert run(["git", "--git-dir", str(remote), "rev-parse", "refs/heads/main"], project)
    assert run(["git", "config", "branch.main.remote"], project) == "origin"
    assert read_gh_calls(calls_path) == [
        ["auth", "status"],
        ["repo", "create", "my-project", "--private", "--source", ".", "--remote", "origin"],
    ]
    assert "Git: initialized repository on main" in out
    assert "GitHub: created origin remote" in out
    assert "First commit: created; push attempted" in out


def test_init_public_and_no_github_options(tmp_path: Path, monkeypatch):
    _, calls_path = install_fake_gh(tmp_path, monkeypatch)
    isolate_missing_git_identity(tmp_path, monkeypatch)
    public_project = tmp_path / "public-project"
    public_project.mkdir()

    run_init_capture(public_project, public=True)

    assert read_gh_calls(calls_path)[-1][3] == "--public"

    no_github_project = tmp_path / "local-project"
    no_github_project.mkdir()
    call_count = len(read_gh_calls(calls_path))
    out = run_init_capture(no_github_project, no_github=True)
    assert len(read_gh_calls(calls_path)) == call_count
    assert "GitHub: skipped (--no-github)" in out


def test_init_not_logged_in_warns_and_still_commits(tmp_path: Path, monkeypatch):
    project = tmp_path / "local-project"
    project.mkdir()
    install_fake_gh(tmp_path, monkeypatch, logged_in=False)
    isolate_missing_git_identity(tmp_path, monkeypatch)

    out = run_init_capture(project)

    expected = "gh repo create local-project --private --source . --remote origin"
    assert expected in out
    assert "not logged in" in out
    assert run(["git", "log", "-1", "--format=%s"], project) == "Initial harness setup"


def test_init_missing_gh_warns_and_still_commits(tmp_path: Path, monkeypatch):
    project = tmp_path / "local-project"
    project.mkdir()
    isolate_missing_git_identity(tmp_path, monkeypatch)
    monkeypatch.setattr("harness.init_project.shutil.which", lambda _command: None)

    out = run_init_capture(project)

    expected = "gh repo create local-project --private --source . --remote origin"
    assert f"gh is unavailable; run `{expected}` later" in out
    assert run(["git", "log", "-1", "--format=%s"], project) == "Initial harness setup"


def test_init_commits_existing_generated_files_when_git_metadata_is_missing(
    tmp_path: Path,
    monkeypatch,
):
    project = tmp_path / "recovered-project"
    project.mkdir()
    isolate_missing_git_identity(tmp_path, monkeypatch)
    run_init_capture(project, no_github=True)
    shutil.move(project / ".git", tmp_path / "removed-git")

    run_init_capture(project, no_github=True)

    assert run(["git", "log", "-1", "--format=%s"], project) == "Initial harness setup"
    assert run(["git", "status", "--porcelain"], project) == ""
    assert set(run(["git", "ls-files"], project).splitlines()) == {
        ".gitignore",
        "AGENTS.md",
        "ARCHITECTURE.md",
        "harness.yaml",
        "tasks/TASK_TEMPLATE.md",
        "tasks/roadmap.yaml",
        "tasks/sprint-01-example/01-example.md",
    }


def test_existing_repo_with_origin_is_not_committed_or_pushed(tmp_path: Path, monkeypatch):
    init_repo(tmp_path)
    write(tmp_path / "existing.txt", "existing\n")
    run(["git", "add", "existing.txt"], tmp_path)
    run(["git", "commit", "-m", "Existing history"], tmp_path)
    remote = tmp_path.parent / f"{tmp_path.name}-remote.git"
    run(["git", "init", "--bare", str(remote)], tmp_path.parent)
    run(["git", "remote", "add", "origin", str(remote)], tmp_path)
    head_before = run(["git", "rev-parse", "HEAD"], tmp_path)
    fake_root = tmp_path.parent / f"{tmp_path.name}-fake-gh"
    fake_root.mkdir()
    _, calls_path = install_fake_gh(fake_root, monkeypatch)

    out = run_init_capture(tmp_path)

    assert run(["git", "rev-parse", "HEAD"], tmp_path) == head_before
    assert read_gh_calls(calls_path) == []
    remote_refs = subprocess.run(
        ["git", "--git-dir", str(remote), "show-ref"],
        check=False,
        text=True,
        capture_output=True,
    )
    assert remote_refs.returncode == 1
    assert "GitHub: skipped (origin exists)" in out
    assert "files are ready to commit" in out
