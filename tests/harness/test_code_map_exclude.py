from __future__ import annotations

from pathlib import Path
from harness.code_map import ensure_code_map_excluded, reset_graphify_availability_cache
from tests.harness.test_code_map import builder_for, run, write
from tests.harness.test_init_project import init_repo, run_init_capture
from tests.portable import make_python_script


def init_repo_without_harness_gitignore(root: Path) -> str:
    run(["git", "init", "-b", "main"], root)
    run(["git", "config", "user.name", "Test"], root)
    run(["git", "config", "user.email", "test@example.com"], root)
    write(root / "source.py", "VALUE = 1\n")
    run(["git", "add", "source.py"], root)
    run(["git", "commit", "-m", "initial"], root)
    return run(["git", "rev-parse", "HEAD"], root)


def git_info_exclude(root: Path) -> Path:
    common = run(["git", "rev-parse", "--git-common-dir"], root)
    path = Path(common)
    if not path.is_absolute():
        path = (root / path).resolve()
    return path / "info" / "exclude"


def stateless_fake_graphify(root: Path) -> tuple[Path, str]:
    body = """import pathlib
import sys

if '--help' in sys.argv:
    print('fake graphify help')
    raise SystemExit(0)
if 'update' not in sys.argv:
    raise SystemExit(0)
out = pathlib.Path.cwd() / 'graphify-out'
out.mkdir(exist_ok=True)
(out / 'graph.json').write_text('{}', encoding='utf-8')
(out / 'manifest.json').write_text('{}', encoding='utf-8')
(out / 'cache').mkdir(exist_ok=True)
"""
    script = make_python_script(root / "fake-graphify.py", body)
    return script, f'"{script}"'


def test_code_map_build_leaves_git_status_clean_without_harness_gitignore(tmp_path: Path):
    init_repo_without_harness_gitignore(tmp_path)
    _script, cmd = stateless_fake_graphify(tmp_path)
    run(["git", "add", "fake-graphify.py"], tmp_path)
    run(["git", "commit", "-m", "add fake graphify"], tmp_path)
    base_sha = run(["git", "rev-parse", "HEAD"], tmp_path)
    reset_graphify_availability_cache()
    builder = builder_for(tmp_path, cmd)

    assert builder.ensure_project_map(base_sha)
    assert run(["git", "status", "--porcelain"], tmp_path) == ""


def test_code_map_exclude_line_is_added_once(tmp_path: Path):
    init_repo_without_harness_gitignore(tmp_path)
    ensure_code_map_excluded(tmp_path)
    ensure_code_map_excluded(tmp_path)
    text = git_info_exclude(tmp_path).read_text(encoding="utf-8")
    assert text.splitlines().count(".harness/code-map/") == 1


def test_init_gitignore_covers_harness_runtime(tmp_path: Path):
    init_repo(tmp_path)
    run_init_capture(tmp_path, no_github=True)
    gitignore = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".harness/" in gitignore.splitlines()
