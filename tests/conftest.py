from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def isolate_graphify_command(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    """Keep default-config tests from invoking a developer's real Graphify."""
    bin_dir = tmp_path_factory.mktemp("test-bin")
    if os.name == "nt":
        shim = bin_dir / "graphify.cmd"
        shim.write_text("@exit /b 1\n", encoding="utf-8")
    else:
        shim = bin_dir / "graphify"
        shim.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        shim.chmod(0o755)

    original_path = os.environ.get("PATH")
    os.environ["PATH"] = os.pathsep.join((str(bin_dir), original_path or ""))
    try:
        yield
    finally:
        if original_path is None:
            os.environ.pop("PATH", None)
        else:
            os.environ["PATH"] = original_path


@pytest.fixture(autouse=True)
def isolate_harness_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """Point machine-wide ledger/outcomes at a fresh temp folder per test."""
    home = tmp_path_factory.mktemp("harness-home")
    monkeypatch.setenv("HARNESS_HOME", str(home))
    yield home


@pytest.fixture(autouse=True)
def isolate_live_provider_limits(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    """Keep default headroom reads away from this machine's provider usage."""
    root = tmp_path_factory.mktemp("provider-homes")
    codex_home = root / "codex"
    claude_home = root / "claude"
    cursor_home = root / "cursor"
    codex_home.mkdir()
    claude_home.mkdir()
    cursor_home.mkdir()

    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_home))
    monkeypatch.setattr(
        "harness.limits.default_cursor_state_db",
        lambda: cursor_home / "state.vscdb",
    )
    yield


@pytest.fixture(autouse=True)
def isolate_default_free_memory(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep default Scheduler builds from reading this machine's free memory."""
    monkeypatch.setattr("harness.scheduler.default_free_memory_gb", lambda: 64.0)
    yield
