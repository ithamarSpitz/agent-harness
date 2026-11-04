from __future__ import annotations

import io
import logging
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

import pytest

from harness.agy_usage import AgyBucket
from harness.config import HarnessConfig
from harness.limits import LimitsPaths, render_limits, run_limits


def _paths(tmp_path: Path) -> LimitsPaths:
    return LimitsPaths(
        codex_home=tmp_path / "codex",
        claude_usage_cache=tmp_path / "claude" / "usage-cache.json",
        cursor_state_db=tmp_path / "cursor.vscdb",
        claude_home=tmp_path / "claude",
    )


def _buckets(*, command: str) -> list[AgyBucket]:
    assert command == "fake-agy"
    return [
        AgyBucket(
            "gemini-weekly",
            "Gemini Models",
            "weekly",
            0.75,
            datetime(2026, 11, 10, 13, 46, 45, tzinfo=timezone.utc),
        ),
        AgyBucket(
            "3p-weekly",
            "Claude and GPT models",
            "weekly",
            0.5,
            datetime(2026, 11, 11, 17, 58, 25, tzinfo=timezone.utc),
        ),
    ]


def test_limits_prints_agy_pools_with_percent_and_local_reset(tmp_path: Path) -> None:
    text = render_limits(
        _paths(tmp_path),
        now=datetime(2026, 11, 4, tzinfo=timezone.utc),
        agy_enabled=True,
        agy_command="fake-agy",
        agy_source=_buckets,
    )

    assert "Antigravity" in text
    assert "Gemini Models: 75% left" in text
    assert "Claude and GPT models: 50% left" in text
    for bucket in _buckets(command="fake-agy"):
        assert bucket.reset_time.astimezone().strftime("%a %b %d, %I:%M %p") in text


def test_limits_prints_one_line_when_agy_read_fails(tmp_path: Path) -> None:
    def fail(*, command: str) -> list[AgyBucket]:
        raise OSError(f"{command} offline")

    text = render_limits(
        _paths(tmp_path),
        agy_enabled=True,
        agy_command="fake-agy",
        agy_source=fail,
    )

    agy_lines = [line for line in text.splitlines() if "Antigravity" in line]
    assert agy_lines == ["Antigravity: usage unavailable (fake-agy offline)"]


def test_disabled_agy_leaves_limits_output_unchanged(tmp_path: Path) -> None:
    calls = 0

    def unused(*, command: str) -> list[AgyBucket]:
        nonlocal calls
        calls += 1
        return []

    baseline = render_limits(_paths(tmp_path), agy_enabled=False)
    actual = render_limits(_paths(tmp_path), agy_enabled=False, agy_source=unused)

    assert actual == baseline
    assert calls == 0


def test_run_limits_reads_enabled_agy_command_from_config(tmp_path: Path) -> None:
    config_path = tmp_path / "harness.yaml"
    config_path.write_text("agy:\n  enabled: true\n  command: fake-agy\n", encoding="utf-8")
    output = io.StringIO()

    with redirect_stdout(output):
        result = run_limits(
            paths=_paths(tmp_path),
            agy_source=_buckets,
            config_path=config_path,
        )

    assert result == 0
    assert "Gemini Models: 75% left" in output.getvalue()


def test_run_limits_uses_explicit_loaded_config_from_another_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config_path = project / "harness.yaml"
    config_path.write_text("agy:\n  enabled: true\n  command: fake-agy\n", encoding="utf-8")
    config = HarnessConfig.load(config_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    output = io.StringIO()

    with redirect_stdout(output):
        run_limits(paths=_paths(tmp_path), agy_source=_buckets, config=config)

    assert "Gemini Models: 75% left" in output.getvalue()
    assert "Claude and GPT models: 50% left" in output.getvalue()


def test_run_limits_reads_config_from_explicit_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (project / "harness.yaml").write_text(
        "agy:\n  enabled: true\n  command: fake-agy\n", encoding="utf-8"
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    output = io.StringIO()

    with redirect_stdout(output):
        run_limits(paths=_paths(tmp_path), agy_source=_buckets, root=project)

    assert "Gemini Models: 75% left" in output.getvalue()


def test_run_limits_default_reads_only_current_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "harness.yaml").write_text(
        "agy:\n  enabled: true\n  command: fake-agy\n", encoding="utf-8"
    )
    sub = tmp_path / "pkg"
    sub.mkdir()
    monkeypatch.chdir(sub)
    calls = 0

    def unused(*, command: str) -> list[AgyBucket]:
        nonlocal calls
        calls += 1
        return []

    output = io.StringIO()
    with redirect_stdout(output):
        run_limits(paths=_paths(tmp_path), agy_source=unused)

    assert "Antigravity" not in output.getvalue()
    assert calls == 0

    monkeypatch.chdir(tmp_path)
    output = io.StringIO()
    with redirect_stdout(output):
        run_limits(paths=_paths(tmp_path), agy_source=_buckets)

    assert "Gemini Models: 75% left" in output.getvalue()


def test_run_limits_warns_once_and_disables_agy_for_broken_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config_path = tmp_path / "harness.yaml"
    config_path.write_text("agy: [unclosed\n", encoding="utf-8")
    calls = 0

    def unused(*, command: str) -> list[AgyBucket]:
        nonlocal calls
        calls += 1
        return []

    output = io.StringIO()
    with caplog.at_level(logging.WARNING, logger="harness.limits"):
        with redirect_stdout(output):
            run_limits(paths=_paths(tmp_path), agy_source=unused, config_path=config_path)

    assert "Antigravity" not in output.getvalue()
    assert calls == 0
    warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert str(config_path) in message
    assert "Antigravity is treated as disabled" in message
