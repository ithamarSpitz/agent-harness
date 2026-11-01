from __future__ import annotations

import json
import subprocess
from pathlib import Path

from harness.cli import main
from tests.portable import file_exists_command, make_python_script, yaml_quote


def run(cmd: list[str], cwd: Path) -> str:
    return subprocess.run(cmd, cwd=cwd, check=True, text=True, capture_output=True).stdout.strip()


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_cli_run_end_to_end_through_fake_cursor_process(tmp_path: Path):
    run(["git", "init", "-b", "main"], tmp_path)
    run(["git", "config", "user.name", "Test"], tmp_path)
    run(["git", "config", "user.email", "test@example.com"], tmp_path)

    fake_agent = make_python_script(
        tmp_path / "fake-agent.py",
        """import pathlib
import sys

args = sys.argv[1:]
if '--version' in args:
    print('fake-agent 1.0')
    raise SystemExit(0)
workspace = pathlib.Path(args[args.index('--workspace') + 1])
if '--mode=ask' in args:
    print('VERDICT: PASS')
else:
    (workspace / 'out.txt').write_text('implemented\\n', encoding='utf-8')
    print('implementation complete')
""",
    )

    verify_out = file_exists_command("out.txt")
    write(tmp_path / ".gitignore", ".harness/\nfake-agent.py\n")
    write(tmp_path / "AGENTS.md", "# test\n")
    write(tmp_path / "harness.yaml", f"""
execution:
  max_parallel_agents: 1
  default_max_attempts: 1
  default_timeout_minutes: 1
  base_ref: main
  keep_successful_worktrees: false
worktree:
  setup_commands: []
  setup_timeout_minutes: 1
cursor:
  command: {yaml_quote(fake_agent)}
  output_format: text
  models: {{worker: null, reviewer: null}}
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths:
  protected: []
  review_required: []
""")
    write(tmp_path / "tasks/A.md", f"""---
allowed_paths: [out.txt]
verification: [{yaml_quote(verify_out)}]
review: true
---
# A
""")
    write(tmp_path / "tasks/roadmap.yaml", f"""
project: test
sprints:
  s1:
    tasks:
      A: {{file: tasks/A.md, stage: 1, depends_on: []}}
    stage_verification:
      "1": [{yaml_quote(verify_out)}]
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    common = [
        "--root", str(tmp_path),
        "--roadmap", "tasks/roadmap.yaml",
        "--config", "harness.yaml",
    ]
    assert main([*common, "doctor"]) == 0
    assert main([*common, "graph", "s1"]) == 0
    assert main([*common, "status", "s1"]) == 0
    assert main([*common, "run", "s1"]) == 0
    assert main([*common, "resume", "s1"]) == 0

    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "implemented\n"
    state = json.loads((tmp_path / ".harness/state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["A"]["status"] == "DONE"
    assert state["meta"]["s1.integration_branch"] == "main"
    assert state["meta"]["s1.configured_base_ref"] == "main"
    log = (tmp_path / ".harness/logs/A-review.log").read_text(encoding="utf-8")
    assert "VERDICT: PASS" in log
    manifests = list((tmp_path / ".harness/runs").glob("s1-*/manifest.json"))
    assert len(manifests) == 1
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert manifest["status"] == "COMPLETED"
    usage_lines = (tmp_path / ".harness/usage.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(usage_lines) == 2


def test_cli_focused_expensive_retry_then_resume(tmp_path: Path):
    run(["git", "init", "-b", "main"], tmp_path)
    run(["git", "config", "user.name", "Test"], tmp_path)
    run(["git", "config", "user.email", "test@example.com"], tmp_path)

    calls = tmp_path / "calls.log"
    fake_agent = make_python_script(
        tmp_path / "fake-agent.py",
        f'''import pathlib
import sys

args = sys.argv[1:]
if '--version' in args:
    print('fake-agent 1.0')
    raise SystemExit(0)
workspace = pathlib.Path(args[args.index('--workspace') + 1])
model = args[args.index('--model') + 1] if '--model' in args else 'none'
with open({str(calls)!r}, 'a', encoding='utf-8') as fh:
    fh.write(model + '\\n')
if '--mode=ask' in args:
    print('VERDICT: PASS')
elif model == 'expensive':
    (workspace / 'out.txt').write_text('fixed\\n', encoding='utf-8')
    print('fixed')
else:
    print('cheap model failed')
    raise SystemExit(7)
''',
    )

    verify_out = file_exists_command("out.txt")
    write(tmp_path / ".gitignore", ".harness/\nfake-agent.py\ncalls.log\n")
    write(tmp_path / "AGENTS.md", "# test\n")
    write(tmp_path / "harness.yaml", f"""
execution:
  max_parallel_agents: 1
  default_max_attempts: 1
  default_timeout_minutes: 1
  base_ref: main
cursor:
  command: {yaml_quote(fake_agent)}
  output_format: text
  models:
    worker: cheap
    escalation: expensive
    reviewer: null
verification:
  global_task_commands: [git diff --check]
  stage_commands: []
paths: {{protected: [], review_required: []}}
""")
    write(tmp_path / "tasks/A.md", f"""---
allowed_paths: [out.txt]
verification: [{yaml_quote(verify_out)}]
review: false
---
# A
""")
    write(tmp_path / "tasks/roadmap.yaml", f"""
project: test
sprints:
  s1:
    tasks:
      A: {{file: tasks/A.md, stage: 1, depends_on: []}}
    stage_verification:
      "1": [{yaml_quote(verify_out)}]
""")
    run(["git", "add", "."], tmp_path)
    run(["git", "commit", "-m", "initial"], tmp_path)

    common = ["--root", str(tmp_path), "--roadmap", "tasks/roadmap.yaml", "--config", "harness.yaml"]
    assert main([*common, "run", "s1"]) == 3
    assert main([*common, "failure", "s1"]) == 0
    state = json.loads((tmp_path / ".harness/state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["A"]["status"] == "BLOCKED"

    assert main([*common, "rerun-blocker", "s1", "--model-class", "escalation"]) == 0
    state = json.loads((tmp_path / ".harness/state.json").read_text(encoding="utf-8"))
    assert state["tasks"]["A"]["status"] == "VERIFIED"

    calls_before_resume = calls.read_text(encoding="utf-8").splitlines()
    assert calls_before_resume == ["cheap", "expensive"]
    assert main([*common, "resume", "s1"]) == 0
    assert calls.read_text(encoding="utf-8").splitlines() == calls_before_resume
    assert (tmp_path / "out.txt").read_text(encoding="utf-8") == "fixed\n"
