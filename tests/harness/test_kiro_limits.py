from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

import harness.limits as limits
from harness.kiro_usage import KiroQuota, KiroUsageError
from harness.limits import LimitsPaths, render_limits, run_limits


RESET = datetime(2026, 12, 1, tzinfo=timezone.utc)
NOW = datetime(2026, 11, 20, tzinfo=timezone.utc)


def _paths(tmp_path: Path) -> LimitsPaths:
    return LimitsPaths(
        codex_home=tmp_path / "codex",
        claude_usage_cache=tmp_path / "claude" / "usage-cache.json",
        cursor_state_db=tmp_path / "cursor.vscdb",
        claude_home=tmp_path / "claude",
    )


@pytest.fixture(autouse=True)
def _stub_existing_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "load_codex", lambda *args, **kwargs: (["Codex"], {}))
    monkeypatch.setattr(limits, "load_cursor", lambda *args, **kwargs: (["Cursor"], {}))
    monkeypatch.setattr(limits, "load_claude", lambda *args, **kwargs: (["Claude"], {}))


def _quota(*, command: list[str] | None) -> KiroQuota:
    assert command == ["fake-kiro", "usage"]
    return KiroQuota("KIRO FREE", 0.2, 50, 0.4, RESET)


def test_enabled_kiro_is_last_in_text_and_present_in_json(tmp_path: Path) -> None:
    kwargs = {
        "now": NOW,
        "kiro_enabled": True,
        "kiro_quota_command": ["fake-kiro", "usage"],
        "kiro_source": _quota,
    }

    text = render_limits(_paths(tmp_path), **kwargs)
    payload = json.loads(render_limits(_paths(tmp_path), as_json=True, **kwargs))

    assert text.endswith(
        "Kiro (free)\n"
        "  credits  0.20 of 50 used (0.4%), resets Tue Dec 01, 2026 (11 days left)"
    )
    assert payload["kiro"] == {
        "limit": 50,
        "plan": "free",
        "resets_at": "2026-12-01T00:00:00+00:00",
        "used": 0.2,
        "used_percent": 0.4,
    }


def test_kiro_usage_error_is_rendered(tmp_path: Path) -> None:
    def fail(*, command: list[str] | None) -> KiroQuota:
        raise KiroUsageError("not logged in")

    text = render_limits(
        _paths(tmp_path),
        kiro_enabled=True,
        kiro_source=fail,
    )
    payload = json.loads(
        render_limits(
            _paths(tmp_path),
            as_json=True,
            kiro_enabled=True,
            kiro_source=fail,
        )
    )

    assert "Kiro\n  unavailable: not logged in" in text
    assert payload["kiro"] == {"error": "not logged in"}


def test_disabled_kiro_has_off_line_and_does_not_call_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness_home = tmp_path / "harness-home"
    monkeypatch.setenv("HARNESS_HOME", str(harness_home))
    calls = 0

    def unused(*, command: list[str] | None) -> KiroQuota:
        nonlocal calls
        calls += 1
        raise AssertionError("disabled Kiro source was called")

    text = render_limits(_paths(tmp_path), kiro_enabled=False, kiro_source=unused)
    payload = json.loads(
        render_limits(
            _paths(tmp_path),
            as_json=True,
            kiro_enabled=False,
            kiro_source=unused,
        )
    )

    assert (
        "Kiro  off (set kiro.enabled: true in harness.yaml or "
        f"{harness_home / 'config.yaml'})" in text
    )
    assert "kiro" not in payload
    assert calls == 0


def test_run_limits_reads_kiro_machine_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harness_home = tmp_path / "harness-home"
    harness_home.mkdir()
    (harness_home / "config.yaml").write_text(
        "kiro:\n  enabled: true\n  quota_command: [fake-kiro, usage]\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HARNESS_HOME", str(harness_home))

    run_limits(paths=_paths(tmp_path), root=tmp_path, kiro_source=_quota)

    assert "Kiro (free)" in capsys.readouterr().out
