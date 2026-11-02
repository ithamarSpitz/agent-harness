from __future__ import annotations

import json
from pathlib import Path

import yaml

from harness.codex_runner import (
    CodexAgentRunner,
    codex_browser_plugins_to_disable,
    codex_plugin_disable_argv,
    codex_playwright_mcp_argv,
)
from harness.config import HarnessConfig
from harness.models import TaskSpec


def _task(tmp_path: Path) -> TaskSpec:
    task_file = tmp_path / "task.md"
    task_file.write_text("# task\n", encoding="utf-8")
    return TaskSpec(id="T1", file=task_file, sprint="S1", stage=1, review=True, allowed_paths=["**"])


def _base_yaml(tmp_path: Path, *, ui_review: dict | None = None, codex_command: str = "codex") -> Path:
    payload: dict = {
        "execution": {
            "stall_timeout_minutes": 0.1,
            "watchdog_poll_seconds": 0.01,
        },
        "cursor": {
            "enabled": True,
            "command": "agent",
            "models": {"worker": "composer-2.5", "reviewer": "composer-2.5"},
        },
        "providers": {"priority": ["codex", "cursor"]},
        "codex": {
            "enabled": True,
            "command": codex_command,
            "implementation_sequence": ["gpt-5.6-sol"],
            "review_model": "gpt-5.6-sol",
        },
        "retry": {"sequence": ["worker"]},
        "verification": {"global_task_commands": []},
        "paths": {"protected": [], "review_required": []},
    }
    if ui_review is not None:
        payload["ui_review"] = ui_review
    path = tmp_path / "harness.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _fake_codex_cli(tmp_path: Path) -> Path:
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
(workspace / "fake-codex-argv.json").write_text(json.dumps(args), encoding="utf-8")
(workspace / "fake-codex-stdin.txt").write_text(prompt, encoding="utf-8")
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"done"}}))
print(json.dumps({"type":"turn.completed","usage":{}}))
''',
        encoding="utf-8",
    )
    return script


def _write_codex_plugins_config(codex_home: Path, plugin_ids: list[str]) -> None:
    codex_home.mkdir(parents=True, exist_ok=True)
    lines = [f'[plugins."{plugin_id}"]\nenabled = true\n' for plugin_id in plugin_ids]
    (codex_home / "config.toml").write_text("".join(lines), encoding="utf-8")


def _playwright_parts(argv: list[str]) -> list[str]:
    return [part for part in argv if "mcp_servers.playwright" in part]


def _plugin_disable_parts(argv: list[str]) -> list[str]:
    return [part for part in argv if part.startswith('plugins."') and part.endswith(".enabled=false")]


def test_codex_playwright_argv_helper():
    argv = codex_playwright_mcp_argv("@playwright/mcp@0.0.84")
    assert 'mcp_servers.playwright.command="npx"' in argv
    assert 'mcp_servers.playwright.args=["-y","@playwright/mcp@0.0.84"]' in argv


def test_browser_plugins_from_fake_user_config(tmp_path: Path):
    codex_home = tmp_path / "codex-home"
    _write_codex_plugins_config(
        codex_home,
        ["chrome@openai-bundled", "browser@openai-bundled", "slack@openai-curated"],
    )
    disabled = codex_browser_plugins_to_disable(codex_home)
    assert "chrome@openai-bundled" in disabled
    assert "browser@openai-bundled" in disabled
    assert "slack@openai-curated" not in disabled


def test_plugin_disable_argv_uses_quoted_plugin_keys():
    argv = codex_plugin_disable_argv(["chrome@openai-bundled", "slack@openai-curated"])
    assert argv == [
        "--config",
        'plugins."chrome@openai-bundled".enabled=false',
        "--config",
        'plugins."slack@openai-curated".enabled=false',
    ]


def test_implement_review_and_plan_repair_argv_with_ui_review(tmp_path: Path, monkeypatch):
    codex_home = tmp_path / "codex-home"
    _write_codex_plugins_config(codex_home, ["slack@openai-curated"])
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    cli = _fake_codex_cli(tmp_path)
    config = HarnessConfig.load(
        _base_yaml(
            tmp_path,
            codex_command=str(cli),
            ui_review={
                "enabled": True,
                "start_commands": ["npm start"],
                "url": "http://localhost:3000",
                "playwright_mcp_package": "@playwright/mcp@0.0.84",
            },
        )
    )
    runner = CodexAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "wt"
    workspace.mkdir()

    runner.implement(_task(tmp_path), workspace, 1, 1, None, model="gpt-5.6-sol")
    implement_argv = json.loads((workspace / "fake-codex-argv.json").read_text(encoding="utf-8"))
    assert _playwright_parts(implement_argv)
    assert 'plugins."chrome@openai-bundled".enabled=false' in implement_argv
    assert not any("slack@openai-curated" in part for part in _plugin_disable_parts(implement_argv))

    runner.review(_task(tmp_path), workspace, "HEAD", "ok", 1, model="gpt-5.6-sol")
    review_argv = json.loads((workspace / "fake-codex-argv.json").read_text(encoding="utf-8"))
    assert _playwright_parts(review_argv)

    runner.plan_change(_task(tmp_path), workspace, "failed", 1, model="gpt-5.6-sol")
    plan_argv = json.loads((workspace / "fake-codex-argv.json").read_text(encoding="utf-8"))
    assert not _playwright_parts(plan_argv)
    assert 'plugins."browser@openai-bundled".enabled=false' in plan_argv


def test_no_playwright_when_ui_review_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty-codex-home"))
    cli = _fake_codex_cli(tmp_path)
    config = HarnessConfig.load(_base_yaml(tmp_path, codex_command=str(cli)))
    runner = CodexAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "wt"
    workspace.mkdir()

    runner.implement(_task(tmp_path), workspace, 1, 1, None, model="gpt-5.6-sol")
    argv = json.loads((workspace / "fake-codex-argv.json").read_text(encoding="utf-8"))
    assert not _playwright_parts(argv)

    runner.review(_task(tmp_path), workspace, "HEAD", "ok", 1, model="gpt-5.6-sol")
    argv = json.loads((workspace / "fake-codex-argv.json").read_text(encoding="utf-8"))
    assert not _playwright_parts(argv)
