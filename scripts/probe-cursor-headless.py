from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from harness.config import HarnessConfig
from harness.git_worktree import WorktreeManager
from harness.runner import CursorAgentRunner


def main() -> int:
    config = HarnessConfig.load(ROOT / "harness.yaml")
    runtime_root = ROOT / ".harness"
    worktrees = WorktreeManager(ROOT, runtime_root)
    worktrees.ensure_repo()
    if not worktrees.is_clean():
        print("Refusing Cursor probe: integration checkout is dirty", file=sys.stderr)
        return 2

    task_id = "__cursor_headless_probe__"
    base_ref = worktrees.head_commit()
    branch, worktree = worktrees.create(task_id, base_ref)
    try:
        runner = CursorAgentRunner(
            config,
            runtime_root / "logs",
            repo_root=ROOT,
            worktree_root=worktrees.worktree_root,
        )
        result = runner._invoke(
            task_id=task_id,
            phase="implement",
            prompt="Reply with exactly: OK. Do not modify files and do not run shell commands.",
            workspace=worktree,
            model_class="worker",
            timeout_minutes=3,
            log_name="cursor-headless-probe.log",
            attempt=0,
        )
        if not result.ok:
            print(result.error or "Cursor headless probe failed", file=sys.stderr)
            if result.output:
                print(result.output, file=sys.stderr)
            return 3
        if result.output.strip().rstrip(".") != "OK":
            print("Cursor headless probe returned unexpected output:", file=sys.stderr)
            print(result.output, file=sys.stderr)
            return 3
        changed = worktrees.changed_files(worktree, base_ref)
        if changed:
            print(f"Cursor probe unexpectedly changed files: {changed}", file=sys.stderr)
            return 3
        print("Cursor headless stdin/trust probe: PASS")
        return 0
    finally:
        worktrees.remove(worktree, branch, delete_branch=True)


if __name__ == "__main__":
    raise SystemExit(main())
