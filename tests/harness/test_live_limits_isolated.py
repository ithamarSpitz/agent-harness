from __future__ import annotations

from pathlib import Path

from harness.headroom import headroom_snapshot
from harness.limits import LimitsPaths


def test_live_provider_limits_are_isolated_from_machine_state() -> None:
    paths = LimitsPaths.from_env()

    assert paths.codex_home != Path.home() / ".codex"
    assert paths.claude_home != Path.home() / ".claude"
    assert paths.codex_home.is_dir()
    assert paths.claude_home is not None
    assert paths.claude_home.is_dir()
    assert paths.cursor_state_db.parent.is_dir()
    assert not paths.cursor_state_db.exists()
    assert paths.codex_home.parent == paths.claude_home.parent
    assert paths.codex_home.parent == paths.cursor_state_db.parent.parent

    snapshot = headroom_snapshot(max_age_seconds=0)

    assert snapshot["codex"]
    assert all(not entry.known for entry in snapshot["codex"])
