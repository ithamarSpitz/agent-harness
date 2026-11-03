"""Emit exact git outputs for S8-T49 review. Safe to delete."""
from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "0e0037127fecd12e6ace79a7026f8a711aa99c33"
OUT = ROOT / "_s8t49_git_outputs.txt"


def main() -> int:
    parts: list[str] = []
    parts.append("===== COMMAND1: git diff =====\n")
    r1 = subprocess.run(
        [
            "git",
            "diff",
            BASE,
            "--",
            "harness/runner.py",
            "harness/codex_runner.py",
            "tests/harness/test_ui_review_prompt_wiring.py",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    parts.append(r1.stdout)
    parts.append("\n===== COMMAND2: findstr runner.py =====\n")
    show_runner = subprocess.run(
        ["git", "show", f"{BASE}:harness/runner.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    for i, line in enumerate(show_runner.stdout.splitlines(), start=1):
        if "implement_prompt" in line or "ui_review" in line:
            parts.append(f"{i}:{line}\n")
    parts.append("\n===== COMMAND3: findstr codex_runner.py =====\n")
    show_codex = subprocess.run(
        ["git", "show", f"{BASE}:harness/codex_runner.py"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    for i, line in enumerate(show_codex.stdout.splitlines(), start=1):
        if "implement_prompt" in line or "ui_review" in line:
            parts.append(f"{i}:{line}\n")
    OUT.write_text("".join(parts), encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
