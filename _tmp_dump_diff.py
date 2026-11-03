"""Temporary helper: emit git-like diffs for the parent agent. Safe to delete."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BASE = "7bd978d38678287cce5e973b2c2fababfe11316d"
OUT = ROOT / "_tmp_git_outputs.txt"


def run(args: list[str]) -> str:
    completed = subprocess.run(
        args,
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def main() -> int:
    parts: list[str] = []
    parts.append("===== CMD 1: git diff BASE --stat =====\n")
    parts.append(run(["git", "diff", BASE, "--stat"]))
    parts.append("\n===== CMD 2: git diff BASE -- scheduler + test =====\n")
    parts.append(
        run(
            [
                "git",
                "diff",
                BASE,
                "--",
                "harness/scheduler.py",
                "tests/harness/test_scheduler_memory.py",
            ]
        )
    )
    parts.append("\n===== CMD 3: memory symbols at BASE in scheduler.py =====\n")
    show = run(["git", "show", f"{BASE}:harness/scheduler.py"])
    keys = (
        "current_memory_estimate",
        "PeakSampler",
        "sampler_factory",
        "_remember_memory_peak",
        "_sample_call",
        "memory",
    )
    lines = show.splitlines()
    for i, line in enumerate(lines, start=1):
        if any(k in line for k in keys):
            start = max(1, i - 5)
            end = min(len(lines), i + 5)
            parts.append(f"--- match around line {i} ---\n")
            for j in range(start, end + 1):
                parts.append(f"{j}:{lines[j - 1]}\n")
            parts.append("\n")
    OUT.write_text("".join(parts), encoding="utf-8")
    print(f"wrote {OUT}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
