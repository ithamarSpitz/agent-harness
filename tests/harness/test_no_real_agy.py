from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from harness.headroom import headroom_snapshot
from harness.limits import LimitsPaths


def _empty_source(_paths: LimitsPaths) -> tuple[list[str], dict[str, Any]]:
    return [], {}


def test_agy_resolves_to_silent_failing_test_shim() -> None:
    executable = shutil.which("agy")

    assert executable is not None
    assert Path(executable).parent.name.startswith("test-bin")
    result = subprocess.run(
        [executable],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_headroom_default_agy_source_uses_failing_test_shim(tmp_path: Path) -> None:
    paths = LimitsPaths(
        codex_home=tmp_path / "codex",
        claude_usage_cache=tmp_path / "claude.json",
        cursor_state_db=tmp_path / "cursor.vscdb",
        claude_home=tmp_path / "claude",
    )

    snapshot = headroom_snapshot(
        max_age_seconds=0,
        paths=paths,
        cache_path=tmp_path / "headroom-cache.json",
        codex_source=_empty_source,
        cursor_source=_empty_source,
        claude_source=_empty_source,
        agy_enabled=True,
        agy_command="agy",
    )

    assert snapshot["agy"]
    assert all(not entry.known for entry in snapshot["agy"])
