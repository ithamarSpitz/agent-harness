from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# When this file is executed directly (``python scripts/probe-codex-provider.py``),
# Python puts ``scripts/`` rather than the repository root on sys.path.  Add the
# root before importing the local Harness package so the smoke probe behaves the
# same way on Windows and POSIX without relying on PYTHONPATH or an installed
# package.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.codex_runner import CodexAgentRunner
from harness.config import HarnessConfig
from harness.usage import UsageRecorder


def run(cmd: list[str], cwd: Path) -> None:
    subprocess.run(cmd, cwd=cwd, check=True, text=True, encoding="utf-8", errors="replace", capture_output=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    root = ROOT
    config = HarnessConfig.load(root / "harness.yaml")
    runner = CodexAgentRunner(config, root / ".harness/logs", usage=UsageRecorder(root / ".harness/usage.jsonl"))
    ok, detail = runner.availability(refresh=True)
    if not ok:
        raise SystemExit(f"Codex auth probe failed: {detail}")
    print(f"Codex auth: PASS ({detail})")
    if not args.live:
        return 0

    with tempfile.TemporaryDirectory(prefix="codex-harness-smoke-") as td:
        repo = Path(td)
        run(["git", "init", "-b", "main"], repo)
        run(["git", "config", "user.name", "Harness Probe"], repo)
        run(["git", "config", "user.email", "probe@example.invalid"], repo)
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        run(["git", "add", "."], repo)
        run(["git", "commit", "-m", "seed"], repo)
        prompt = (
            "You are a smoke probe. Work only in this repository.\n"
            "1. Verify your current working directory using a shell command.\n"
            "2. Create probe.txt containing exactly CODEX_HARNESS_OK followed by a newline.\n"
            "3. Run a shell command that reads probe.txt and verifies the exact content.\n"
            "4. Reply with exactly: PROBE PASS\n"
        )
        model, reasoning_effort = config.codex.implementation_profile(0)
        result = runner._invoke(
            task_id="CODEX-SMOKE", phase="implement", prompt=prompt, workspace=repo,
            model=model, reasoning_effort=reasoning_effort, timeout_minutes=5,
            log_name="codex-provider-smoke.log", env=os.environ.copy(),
        )
        if not result.ok or result.output.strip() != "PROBE PASS":
            raise SystemExit(f"Codex {model} {reasoning_effort} smoke failed: {result.error or result.output}")
        probe = repo / "probe.txt"
        try:
            if not probe.is_file():
                kind = "directory" if probe.is_dir() else "missing/non-file"
                raise SystemExit(f"Codex edit/cwd/tool smoke failed: probe.txt is {kind}")
            content = probe.read_text(encoding="utf-8")
        except PermissionError as exc:
            raise SystemExit(
                "Codex edit/cwd/tool smoke failed: host process cannot read probe.txt after Codex exited; "
                f"Windows sandbox/ACL issue suspected: {exc}"
            ) from exc
        if content != "CODEX_HARNESS_OK\n":
            raise SystemExit(f"Codex edit/cwd/tool smoke failed: probe.txt mismatch: {content!r}")
        print(f"Codex {model} {reasoning_effort} exec/cwd/edit/tool smoke: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
