from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest
import yaml

from harness.codex_runner import CodexAgentRunner
from harness.config import HarnessConfig
from harness.impact import (
    MAX_IMPACT_LINES,
    MAX_USERS_PER_SYMBOL,
    NO_CODE_MAP_GREP_HEADER,
    OUTSIDE_IMPACT_HEADER,
    UNREADABLE_CODE_MAP_GREP_HEADER,
    collect_outside_impact,
    find_changed_symbols,
    format_outside_impact_lines,
)
from harness.models import TaskSpec
from harness.provider_runner import ProviderAgentRunner
from harness.runner import CursorAgentRunner
from harness.state import StateStore


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _init_fixture(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-b", "main"], repo)
    run(["git", "config", "user.name", "Test"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    write(repo / "lib.py", "def f():\n    return 1\n")
    write(repo / "caller.py", "from lib import f\n\ndef use():\n    return f()\n")
    write(repo / ".gitignore", ".harness/\n")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "initial"], repo)
    base = run(["git", "rev-parse", "HEAD"], repo)
    write(repo / "lib.py", "def f():\n    return 2\n")
    return repo, base


def _fake_graph(repo: Path) -> None:
    graph = {
        "nodes": [
            {
                "id": "lib_f",
                "label": "f()",
                "source_file": "lib.py",
                "source_location": "L1",
                "_callable": True,
            },
            {
                "id": "caller_use",
                "label": "use()",
                "source_file": "caller.py",
                "source_location": "L3",
                "_callable": True,
            },
        ],
        "links": [
            {
                "source": "caller_use",
                "target": "lib_f",
                "relation": "calls",
                "source_file": "caller.py",
                "source_location": "L4",
            }
        ],
    }
    out = repo / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text(json.dumps(graph), encoding="utf-8")


def _fake_codex(tmp_path: Path) -> Path:
    script = tmp_path / "fake-codex.py"
    script.write_text(
        r'''from __future__ import annotations
import json, sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.154.0")
    raise SystemExit(0)
if args == ["login", "status"]:
    print("Logged in using ChatGPT")
    raise SystemExit(0)
if not args or args[0] != "exec":
    raise SystemExit(2)

prompt = sys.stdin.read()
workspace = Path(args[args.index("--cd") + 1])
(workspace / "fake-codex-stdin.txt").write_text(prompt, encoding="utf-8")
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"VERDICT: PASS"}}))
print(json.dumps({"type":"turn.completed","usage":{}}))
''',
        encoding="utf-8",
    )
    return script


def _provider(repo: Path, codex_cli: Path) -> ProviderAgentRunner:
    config_path = repo / "harness.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "cursor": {
                    "enabled": True,
                    "command": "agent",
                    "models": {"worker": "composer-2.5", "reviewer": "composer-2.5"},
                },
                "providers": {"priority": ["codex", "cursor"]},
                "codex": {
                    "enabled": True,
                    "command": str(codex_cli),
                    "implementation_sequence": ["gpt-5.6-sol"],
                    "review_model": "gpt-5.6-sol",
                },
                "retry": {"sequence": ["worker"]},
                "verification": {"global_task_commands": []},
                "paths": {"protected": [], "review_required": []},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    config = HarnessConfig.load(config_path)
    logs = repo / ".harness" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    state = StateStore(repo / ".harness" / "state.json")
    codex = CodexAgentRunner(config, logs)
    cursor = CursorAgentRunner(config, logs, repo_root=repo, worktree_root=repo / ".harness" / "worktrees")
    return ProviderAgentRunner(config, state, codex, cursor)


def _task(repo: Path) -> TaskSpec:
    brief = repo / "task.md"
    brief.write_text("# task\n", encoding="utf-8")
    return TaskSpec(id="T16", file=brief, sprint="S9", stage=1, review=True, allowed_paths=["**"])


def test_changed_symbols_detects_modified_function(tmp_path: Path) -> None:
    repo, base = _init_fixture(tmp_path)
    symbols = find_changed_symbols(repo, base)
    assert any(s.name == "f" and s.file == "lib.py" for s in symbols)


def test_graph_lists_outside_caller(tmp_path: Path) -> None:
    repo, base = _init_fixture(tmp_path)
    _fake_graph(repo)
    body = collect_outside_impact(repo, base)
    assert "caller.py:4 -> f" in body


def test_grep_fallback_without_map(tmp_path: Path) -> None:
    repo, base = _init_fixture(tmp_path)
    body = collect_outside_impact(repo, base)
    lines = body.splitlines()
    assert lines[0] == NO_CODE_MAP_GREP_HEADER
    assert "caller.py:" in body
    assert "-> f" in body


def test_broken_graph_falls_back_to_grep_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo, base = _init_fixture(tmp_path)
    out = repo / "graphify-out"
    out.mkdir(parents=True, exist_ok=True)
    (out / "graph.json").write_text("{not-json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="harness.impact"):
        body = collect_outside_impact(repo, base)
    lines = body.splitlines()
    assert lines[0] == UNREADABLE_CODE_MAP_GREP_HEADER
    assert "caller.py:" in body
    assert "-> f" in body
    assert any("failed to read code map for review impact" in r.message for r in caplog.records)


def test_grep_fallback_skips_markdown_prefers_harness(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-b", "main"], repo)
    run(["git", "config", "user.name", "Test"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    write(repo / "pkg" / "api.py", "def run():\n    return 1\n")
    write(repo / "harness" / "caller.py", "from pkg.api import run\n\ndef use():\n    return run()\n")
    write(repo / "tests" / "test_api.py", "from pkg.api import run\n\ndef test_run():\n    assert run() == 1\n")
    write(repo / "README.md", "Call run() to start.\n")
    write(repo / ".gitignore", ".harness/\n")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "initial"], repo)
    base = run(["git", "rev-parse", "HEAD"], repo)
    write(repo / "pkg" / "api.py", "def run():\n    return 2\n")

    body = collect_outside_impact(repo, base)
    lines = [line for line in body.splitlines() if " -> " in line]
    assert lines[0].startswith("harness/caller.py:")
    assert any(line.startswith("tests/test_api.py:") for line in lines)
    assert "README.md" not in body
    assert body.splitlines()[0] == NO_CODE_MAP_GREP_HEADER


def test_grep_fallback_caps_users_per_symbol(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    run(["git", "init", "-b", "main"], repo)
    run(["git", "config", "user.name", "Test"], repo)
    run(["git", "config", "user.email", "test@example.com"], repo)
    write(repo / "lib.py", "def helper():\n    return 1\n")
    for i in range(8):
        write(
            repo / "tests" / f"user_{i}.py",
            f"from lib import helper\n\ndef use_{i}():\n    return helper()\n",
        )
    write(repo / ".gitignore", ".harness/\n")
    run(["git", "add", "."], repo)
    run(["git", "commit", "-m", "initial"], repo)
    base = run(["git", "rev-parse", "HEAD"], repo)
    write(repo / "lib.py", "def helper():\n    return 2\n")

    body = collect_outside_impact(repo, base)
    user_lines = [line for line in body.splitlines() if " -> helper" in line]
    assert len(user_lines) == MAX_USERS_PER_SYMBOL


def test_impact_lines_capped_with_count() -> None:
    lines = [f"other.py:{i} -> f" for i in range(1, 46)]
    formatted = format_outside_impact_lines(lines)
    out_lines = formatted.splitlines()
    assert len(out_lines) == MAX_IMPACT_LINES + 1
    assert out_lines[-1] == "and 5 more"
    assert out_lines[0] == "other.py:1 -> f"


def test_review_prompt_via_provider_includes_graph_caller(tmp_path: Path) -> None:
    repo, base = _init_fixture(tmp_path)
    _fake_graph(repo)
    cli = _fake_codex(tmp_path)
    runner = _provider(repo, cli)
    result = runner.review(_task(repo), repo, base, "ok", 1)
    assert result.ok
    prompt = (repo / "fake-codex-stdin.txt").read_text(encoding="utf-8")
    assert OUTSIDE_IMPACT_HEADER in prompt
    assert "caller.py:4 -> f" in prompt
    assert prompt.index(OUTSIDE_IMPACT_HEADER) < prompt.index(
        "Before the verdict, complete this required impact check:"
    )


def test_review_prompt_via_provider_grep_fallback(tmp_path: Path) -> None:
    repo, base = _init_fixture(tmp_path)
    cli = _fake_codex(tmp_path)
    runner = _provider(repo, cli)
    result = runner.review(_task(repo), repo, base, "ok", 1)
    assert result.ok
    prompt = (repo / "fake-codex-stdin.txt").read_text(encoding="utf-8")
    assert OUTSIDE_IMPACT_HEADER in prompt
    assert NO_CODE_MAP_GREP_HEADER in prompt
    assert "caller.py:" in prompt
    assert "-> f" in prompt
