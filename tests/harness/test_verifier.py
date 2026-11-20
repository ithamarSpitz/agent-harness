from pathlib import Path

from harness.models import TaskSpec
from harness.scheduler import Scheduler
from harness.verifier import _matches


def test_scope_glob_prefix():
    assert _matches("src/services/a.ts", "src/**")
    assert _matches("src/services/a.ts", "src/services/**")
    assert not _matches("tests/a.ts", "src/**")


def test_exact_path():
    assert _matches("package.json", "package.json")
    assert not _matches("package-lock.json", "package.json")

def _task(task_id: str, patterns: list[str]) -> TaskSpec:
    return TaskSpec(id=task_id, file=Path("x"), sprint="s", stage=1, allowed_paths=patterns)


def test_parallel_scope_conflict_detection():
    assert Scheduler._tasks_may_conflict(_task("A", ["src/services/**"]), _task("B", ["src/services/x.ts"]))
    assert not Scheduler._tasks_may_conflict(_task("A", ["src/a/**"]), _task("B", ["src/b/**"]))
    assert Scheduler._tasks_may_conflict(_task("A", []), _task("B", ["src/b/**"]))


def test_review_required_paths_force_review_match(tmp_path):
    from harness.config import HarnessConfig
    from harness.git_worktree import WorktreeManager
    from harness.verifier import Verifier

    (tmp_path / "harness.yaml").write_text("""
execution: {}
worktree: {}
cursor: {models: {}}
verification: {}
paths:
  protected: []
  review_required:
    - ARCHITECTURE.md
    - prisma/**
""", encoding="utf-8")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    verifier = Verifier(config, tmp_path / "logs", WorktreeManager(tmp_path, tmp_path / ".harness"))
    assert verifier.requires_review(["ARCHITECTURE.md"])
    assert verifier.requires_review(["prisma/schema.prisma"])
    assert not verifier.requires_review(["src/a.ts"])


def test_verification_env_supplies_missing_database_url_without_overriding_real_env(tmp_path, monkeypatch):
    from harness.config import HarnessConfig
    from harness.git_worktree import WorktreeManager
    from harness.verifier import Verifier

    (tmp_path / "harness.yaml").write_text("""
execution: {}
worktree: {}
cursor: {models: {}}
verification:
  env:
    DATABASE_URL: postgresql://validation:validation@127.0.0.1:1/validation
paths: {protected: [], review_required: []}
""", encoding="utf-8")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    verifier = Verifier(config, tmp_path / "logs", WorktreeManager(tmp_path, tmp_path / ".harness"))

    # Ambient shell state must not leak back in when the caller deliberately
    # supplies an explicit verification environment. This reproduces the
    # Windows failure that originally exposed the bug.
    monkeypatch.setenv("DATABASE_URL", "postgresql://ambient:ambient@localhost:5432/ambient")

    fallback = verifier._verification_env({"PATH": "x"})
    assert fallback["DATABASE_URL"].startswith("postgresql://validation:")

    real = verifier._verification_env({"DATABASE_URL": "postgresql://real:real@postgres:5432/app"})
    assert real["DATABASE_URL"] == "postgresql://real:real@postgres:5432/app"

    inherited = verifier._verification_env(None)
    assert inherited["DATABASE_URL"] == "postgresql://ambient:ambient@localhost:5432/ambient"


def test_stage_subprocess_uses_utf8_replacement_decoding(tmp_path, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    from harness.config import HarnessConfig
    from harness.git_worktree import WorktreeManager
    from harness.verifier import Verifier

    (tmp_path / "harness.yaml").write_text("""
execution: {}
worktree: {}
cursor: {models: {}}
verification: {env: {}}
paths: {protected: [], review_required: []}
""", encoding="utf-8")
    config = HarnessConfig.load(tmp_path / "harness.yaml")
    verifier = Verifier(config, tmp_path / "logs", WorktreeManager(tmp_path, tmp_path / ".harness"))
    seen = {}
    def fake_run(*args, **kwargs):
        seen.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")
    monkeypatch.setattr(subprocess, "run", fake_run)
    result = verifier.verify_stage(tmp_path, ["echo ok"], "stage")
    assert result.ok
    assert seen["encoding"] == "utf-8"
    assert seen["errors"] == "replace"


def test_verify_task_attempt_logs_and_latest(tmp_path):
    import subprocess
    import sys
    from types import SimpleNamespace

    from harness.config import HarnessConfig
    from harness.console_commands import _latest_agent_log
    from harness.git_worktree import WorktreeManager
    from harness.verifier import Verifier

    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    (tmp_path / "f.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=tmp_path, check=True, capture_output=True)
    cfg = tmp_path / "harness.yaml"
    cfg.write_text("verification: {global_task_commands: [], stage_commands: []}\n", encoding="utf-8")
    verifier = Verifier(
        HarnessConfig.load(cfg), tmp_path / ".harness" / "logs",
        WorktreeManager(tmp_path, tmp_path / ".harness"),
    )
    marker = [sys.executable, "-c", "print('MARK')"]
    task = TaskSpec(id="T", file=Path("T.md"), sprint="s", stage=1, verification=[subprocess.list2cmdline(marker)])

    verifier.verify_task(task, tmp_path, "HEAD", attempt=1)
    agent_log = tmp_path / ".harness" / "logs" / "T-attempt-2.log"
    agent_log.write_text("agent output", encoding="utf-8")
    task.verification = [subprocess.list2cmdline([sys.executable, "-c", "raise SystemExit(1)"])]
    verifier.verify_task(task, tmp_path, "HEAD", attempt=2)

    logs = tmp_path / ".harness" / "logs"
    one = (logs / "T-attempt-1-verification.log").read_text(encoding="utf-8")
    two = (logs / "T-attempt-2-verification.log").read_text(encoding="utf-8")
    assert "Verification command failed" not in one
    assert (logs / "T-verification.log").read_text(encoding="utf-8") == two
    context = SimpleNamespace(runtime=SimpleNamespace(root=tmp_path))
    for _ in range(20):
        assert _latest_agent_log(context, "T") == agent_log

    verifier.verify_task(task, tmp_path, "HEAD")
    assert not (logs / "T-attempt-None-verification.log").exists()
