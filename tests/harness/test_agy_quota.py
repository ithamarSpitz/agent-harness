from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.agy_runner import AgyAgentRunner
from harness.runner import AgentResult
from tests.harness.test_agy_runner import _invoke_review, _task, _write_config
from tests.portable import make_python_script


def _fake(tmp_path: Path, line: str, *, success: bool = False):  # type: ignore[no-untyped-def]
    if success:
        stream = json.dumps(
            {"event": "result", "result": {"status": "SUCCESS", "response": line}}
        )
        body = f"    print({stream!r})\n"
    else:
        body = f"    print({line!r})\n    raise SystemExit(1)\n"
    return make_python_script(
        tmp_path / "fake-agy.py",
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('agy fake')\n"
        "else:\n"
        "    _ = sys.stdin.read()\n" + body,
    )


CASES = [
    ("Your AI credits balance is too low to continue.", "quota"),
    ("credits balance is too low", "quota"),
    ("Your credits balance is too low", "quota"),
    ("Your AI credits balance is too low.", "quota"),
    ("Your AI credits balance isn’t enough", "quota"),
    ("Your AI credits balance isn't enough", "quota"),
    ("AI: OUT OF CREDITS", "quota"),
    ("Quota exhausted", "quota"),
    ("RESOURCE_EXHAUSTED", "quota"),
    ("model capacity exhausted", "provider_busy"),
    ("Rate limit hit", "provider_busy"),
    ("connection refused", "network"),
    ("HTTP 503 service unavailable", "provider_error"),
    ("Not signed in", "auth"),
    ("please login again", "auth"),
    ("OAuth token rejected", "auth"),
]


@pytest.mark.parametrize(("text", "kind"), CASES)
def test_failed_call_outcome(tmp_path: Path, text: str, kind: str) -> None:
    fake = _fake(tmp_path, text)
    runner = AgyAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")
    result = _invoke_review(runner, tmp_path, "X")
    assert not result.ok
    assert result.quota_exhausted is (kind == "quota")
    assert result.auth_failure is (kind == "auth")
    if kind in {"quota", "auth"}:
        assert result.waiting_cause is None
    else:
        assert result.waiting_cause == kind


@pytest.mark.parametrize(("text", "kind"), CASES)
def test_successful_answer_with_text_does_nothing(
    tmp_path: Path, text: str, kind: str
) -> None:
    fake = _fake(tmp_path, text, success=True)
    runner = AgyAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")
    result = _invoke_review(runner, tmp_path, "Y")
    assert result.ok
    assert not result.quota_exhausted and not result.auth_failure
    assert result.waiting_cause is None


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Your AI credits balance is too low", "quota"),
        ("model capacity exhausted", "provider_busy"),
        ("connection refused", "network"),
        ("HTTP 503", "provider_error"),
    ],
)
def test_json_non_success_result_is_classified(
    tmp_path: Path, text: str, kind: str
) -> None:
    stream = json.dumps(
        {"event": "result", "result": {"status": "ERROR", "response": text}}
    )
    fake = make_python_script(
        tmp_path / "fake-agy.py",
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('agy fake')\n"
        "else:\n"
        "    _ = sys.stdin.read()\n"
        f"    print({stream!r})\n",
    )
    runner = AgyAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")
    result = _invoke_review(runner, tmp_path, "J")
    assert not result.ok
    assert result.quota_exhausted is (kind == "quota")
    if kind == "quota":
        assert result.waiting_cause is None
    else:
        assert result.waiting_cause == kind


def test_quota_text_wins_over_timeout(tmp_path: Path) -> None:
    fake = make_python_script(
        tmp_path / "fake-agy-idle.py",
        "import sys, time\n"
        "if '--version' in sys.argv:\n"
        "    print('agy fake')\n"
        "else:\n"
        "    _ = sys.stdin.read()\n"
        "    print('Quota exhausted', flush=True)\n"
        "    time.sleep(5)\n",
    )
    runner = AgyAgentRunner(
        _write_config(
            tmp_path,
            str(fake),
            stall_timeout_minutes=0.001,
            watchdog_poll_seconds=0.01,
        ),
        tmp_path / "logs",
    )
    result = _invoke_review(runner, tmp_path, "T")
    assert not result.ok
    assert result.quota_exhausted
    assert result.waiting_cause is None


def test_quota_retries_on_other_pool_then_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = AgyAgentRunner(_write_config(tmp_path), tmp_path / "logs")
    models: list[str] = []

    def fake_invoke(**kwargs: object) -> AgentResult:
        model = str(kwargs["model"])
        models.append(model)
        pool = runner._pool_for_model(model)
        return AgentResult(False, "", "quota", quota_exhausted=True,
                           quota_scope=pool, model=model, provider="agy")

    monkeypatch.setattr(runner, "_invoke", fake_invoke)
    result = runner.implement(_task(tmp_path), tmp_path, 1, 1, None)
    assert models == ["gemini-low", "claude-opus"]
    assert result.quota_exhausted and result.quota_scope == "3p-weekly"

    # No other pool left: a single call, reported as out of quota.
    models.clear()
    result = runner.implement(
        _task(tmp_path), tmp_path, 1, 3, None, exhausted_pools=["3p-weekly"]
    )
    assert len(models) == 1
    assert result.quota_exhausted
