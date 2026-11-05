from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from harness.agy_runner import AgyAgentRunner
from harness.config import HarnessConfig
from harness.models import TaskSpec
from harness.usage import UsageRecorder
from tests.portable import make_python_script


FIXTURE = Path(__file__).parent / "fixtures" / "agy-stream-sample.jsonl"


def _write_config(
    tmp_path: Path,
    agy_command: str = "agy",
    *,
    stall_timeout_minutes: float = 12.0,
    watchdog_poll_seconds: float = 10.0,
    effort_models: list[str] | None = None,
) -> HarnessConfig:
    payload = {
        "execution": {
            "max_parallel_agents": 1,
            "default_max_attempts": 2,
            "default_timeout_minutes": 1,
            "stall_timeout_minutes": stall_timeout_minutes,
            "watchdog_poll_seconds": watchdog_poll_seconds,
        },
        "worktree": {},
        "code_map": {"command": "graphify"},
        "cursor": {"models": {"worker": "test-model"}},
        "agy": {
            "enabled": True,
            "command": agy_command,
            "implementation_sequence": [
                {"model": "gemini-low", "effort": "low"},
                {"model": "gemini-high", "effort": "high"},
                {"model": "claude-opus", "effort": "xhigh"},
            ],
            "review_model": "claude-review",
            "review_effort": "high",
            "escalation_model": "claude-opus",
            "escalation_effort": "max",
            "effort_models": ["gemini-"] if effort_models is None else effort_models,
            "pools": {
                "gemini-weekly": ["gemini-"],
                "3p-weekly": ["claude-"],
            },
            "base_allow": ["command(git status*)", "command(git diff*)"],
        },
        "verification": {"global_task_commands": [], "stage_commands": []},
        "paths": {"protected": [], "review_required": []},
    }
    path = tmp_path / "harness.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return HarnessConfig.load(path)


def _task(tmp_path: Path) -> TaskSpec:
    brief = tmp_path / "brief.md"
    brief.write_text("# Brief\n", encoding="utf-8")
    return TaskSpec(
        id="S10-T4",
        file=brief,
        sprint="S10",
        stage=2,
        model_class="hard_worker",
        verification=["python -m pytest -q tests/harness"],
    )


def _settings() -> str:
    return '{"permissions":{"allow":[]}}'


def _expected_command(
    command: str,
    model: str,
    effort: str | None,
    mode: str,
    *,
    accepts_effort: bool = True,
) -> list[str]:
    result = [
        command,
        "-p=",
        "--input-format",
        "stream-json",
        "--output-format",
        "stream-json",
        "--model",
        model,
    ]
    if effort and accepts_effort:
        result += ["--effort", effort]
    return result + ["--mode", mode, "--settings", _settings()]


@pytest.mark.parametrize(
    ("attempt", "model", "effort", "accepts_effort"),
    [
        (1, "gemini-low", "low", True),
        (2, "gemini-high", "high", True),
        (3, "claude-opus", "xhigh", False),
        (4, "claude-opus", "xhigh", False),
    ],
)
def test_agy_command_line_for_implementation_attempts(
    tmp_path: Path,
    attempt: int,
    model: str,
    effort: str,
    accepts_effort: bool,
) -> None:
    runner = AgyAgentRunner(_write_config(tmp_path), tmp_path / "logs")
    profile = runner._implementation_profile(attempt)
    assert profile == (model, effort)
    argv = runner._command(*profile, "accept-edits", _settings())
    assert argv == _expected_command(
        "agy", model, effort, "accept-edits", accepts_effort=accepts_effort
    )
    # agy 1.5.0 takes the token after a standalone ``-p`` as the prompt, so
    # print mode must be the attached empty form in a single argument.
    assert argv[1] == "-p="
    assert "-p" not in argv
    assert "--sandbox" not in argv
    assert "--dangerously-skip-permissions" not in argv


def test_agy_command_line_for_review_escalation_and_pool_skip(tmp_path: Path) -> None:
    runner = AgyAgentRunner(_write_config(tmp_path), tmp_path / "logs")

    review = runner._review_profile()
    assert review == ("claude-review", "high")
    assert runner._command(*review, "plan", _settings()) == _expected_command(
        "agy", "claude-review", "high", "plan", accepts_effort=False
    )
    assert "--dangerously-skip-permissions" not in runner._command(
        *review, "plan", _settings()
    )

    escalation = runner._implementation_profile(1, model_class="escalation")
    assert escalation == ("claude-opus", "max")
    assert runner._command(
        *escalation, "accept-edits", _settings()
    ) == _expected_command(
        "agy", "claude-opus", "max", "accept-edits", accepts_effort=False
    )

    assert runner._implementation_profile(
        1, exhausted_pools={"gemini-weekly"}
    ) == ("claude-opus", "xhigh")


def test_agy_empty_effort_models_omits_effort_for_every_model(
    tmp_path: Path,
) -> None:
    runner = AgyAgentRunner(
        _write_config(tmp_path, effort_models=[]), tmp_path / "logs"
    )

    assert runner._command(
        "gemini-high", "high", "accept-edits", _settings()
    ) == _expected_command(
        "agy", "gemini-high", "high", "accept-edits", accepts_effort=False
    )


def test_agy_logs_when_configured_effort_is_omitted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    runner = AgyAgentRunner(_write_config(tmp_path), tmp_path / "logs")

    with caplog.at_level("DEBUG", logger="harness.agy_runner"):
        runner._command("claude-review", "high", "plan", _settings())

    assert "Omitting agy --effort for model claude-review" in caplog.text


def test_agy_stream_fixture_extracts_answer_usage_and_success(tmp_path: Path) -> None:
    runner = AgyAgentRunner(_write_config(tmp_path), tmp_path / "logs")

    answer, status, usage, denied, diagnostics = runner._parse_output(
        FIXTURE.read_text(encoding="utf-8")
    )

    assert answer == "ok\n"
    assert status == "SUCCESS"
    assert usage == {
        "input_tokens": 7726,
        "output_tokens": 1,
        "thinking_tokens": 0,
        "cache_read_tokens": 0,
    }
    assert denied == []
    assert diagnostics == ""


def _result_stream(
    *, status: str = "SUCCESS", denied_actions: list[dict[str, str]] | None = None
) -> str:
    return json.dumps(
        {
            "event": "result",
            "result": {
                "conversation_id": "fake",
                "status": status,
                "response": "answer",
                "duration_seconds": 0.1,
                "num_turns": 1,
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 3,
                    "thinking_tokens": 2,
                    "cache_read_tokens": 1,
                    "total_tokens": 9,
                },
                "denied_actions": denied_actions or [],
            },
        }
    )


def _fake_result_runner(
    tmp_path: Path, stream: str
) -> tuple[AgyAgentRunner, UsageRecorder]:
    fake = make_python_script(
        tmp_path / "fake-agy.py",
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('agy fake-1.0')\n"
        "else:\n"
        "    _ = sys.stdin.read()\n"
        f"    print({stream!r})\n",
    )
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    return (
        AgyAgentRunner(
            _write_config(tmp_path, str(fake)), tmp_path / "logs", usage=usage
        ),
        usage,
    )


def test_agy_denied_actions_fail_and_are_listed(tmp_path: Path) -> None:
    runner, usage = _fake_result_runner(
        tmp_path,
        _result_stream(
            denied_actions=[
                {"action": "command(npm test)", "display_name": "Run npm test"},
                {"action": "command(git status)", "display_name": ""},
            ]
        ),
    )

    result = runner._invoke(
        task_id="DENIED",
        phase="implement",
        prompt="test",
        workspace=tmp_path,
        model="gemini-low",
        effort="low",
        mode="accept-edits",
        settings=_settings(),
        timeout_minutes=1,
        log_name="denied.log",
        attempt=1,
    )

    assert not result.ok
    assert result.output == "answer"
    assert result.error == (
        "Agy denied actions: Run npm test (command(npm test)), command(git status)"
    )
    event = usage.events()[0]
    assert event["provider"] == "agy"
    assert event["model"] == "gemini-low"
    assert event["pool_id"] == "gemini-weekly"
    assert event["input_tokens"] == 4
    assert event["output_tokens"] == 3
    assert event["reasoning_output_tokens"] == 2
    assert event["cached_input_tokens"] == 1


def test_agy_non_success_status_fails(tmp_path: Path) -> None:
    runner, _ = _fake_result_runner(tmp_path, _result_stream(status="ERROR"))
    result = runner._invoke(
        task_id="ERROR",
        phase="review",
        prompt="test",
        workspace=tmp_path,
        model="claude-review",
        effort="high",
        mode="plan",
        settings=_settings(),
        timeout_minutes=1,
        log_name="error.log",
    )

    assert not result.ok
    assert result.error == "Agy reported status ERROR"


def test_agy_missing_command_is_launch_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = AgyAgentRunner(_write_config(tmp_path), tmp_path / "logs")
    monkeypatch.setattr("harness.agy_runner.locate", lambda _command: None)

    result = _invoke_review(runner, tmp_path, "MISSING")

    message = (
        "agy not found on PATH (installed after this shell started? restart the shell or "
        "set agy.command to the full path)"
    )
    assert not result.ok
    assert result.launch_failure
    assert result.output == message
    assert message in str(result.error)


def _invoke_review(runner: AgyAgentRunner, tmp_path: Path, name: str):  # type: ignore[no-untyped-def]
    return runner._invoke(
        task_id=name,
        phase="review",
        prompt="test",
        workspace=tmp_path,
        model="claude-review",
        effort="high",
        mode="plan",
        settings=_settings(),
        timeout_minutes=1,
        log_name=f"{name}.log",
    )


def test_agy_successful_answer_mentioning_limit_phrases_is_not_quota(
    tmp_path: Path,
) -> None:
    answer = "VERDICT: PASS rate limit, Quota exhausted, RESOURCE_EXHAUSTED, model capacity exhausted"
    stream = "\n".join(
        (
            json.dumps({"event": "text_delta", "text": answer}),
            json.dumps(
                {
                    "event": "result",
                    "result": {
                        "status": "SUCCESS",
                        "response": answer,
                        "usage": {},
                        "denied_actions": [],
                    },
                }
            ),
        )
    )
    fake = make_python_script(
        tmp_path / "fake-agy-words.py",
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('agy fake-1.0')\n"
        "else:\n"
        "    _ = sys.stdin.read()\n"
        f"    print({stream!r})\n",
    )
    runner = AgyAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    result = _invoke_review(runner, tmp_path, "WORDS")

    assert result.ok
    assert not result.quota_exhausted
    assert not result.capacity_exhausted
    assert result.quota_scope is None
    assert result.error is None
    assert result.output == answer


@pytest.mark.parametrize(
    ("line", "quota", "capacity"),
    [
        ("Your AI credits balance is too low to continue.", True, False),
        ("RESOURCE_EXHAUSTED: Quota exhausted", True, False),
        ("model capacity exhausted", False, True),
    ],
)
def test_agy_failed_call_with_limit_text_is_classified(
    tmp_path: Path, line: str, quota: bool, capacity: bool
) -> None:
    fake = make_python_script(
        tmp_path / "fake-agy-limit.py",
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('agy fake-1.0')\n"
        "else:\n"
        "    _ = sys.stdin.read()\n"
        f"    print({line!r})\n"
        "    raise SystemExit(1)\n",
    )
    runner = AgyAgentRunner(_write_config(tmp_path, str(fake)), tmp_path / "logs")

    result = _invoke_review(runner, tmp_path, "LIMIT")

    assert not result.ok
    assert result.quota_exhausted is quota
    assert result.capacity_exhausted is capacity
    assert (result.quota_scope is not None) is quota


def test_agy_nonzero_exit_fails_even_with_success_result(tmp_path: Path) -> None:
    stream = _result_stream()
    fake = make_python_script(
        tmp_path / "fake-agy-exit.py",
        "import sys\n"
        "if '--version' in sys.argv:\n"
        "    print('agy fake-1.0')\n"
        "else:\n"
        "    _ = sys.stdin.read()\n"
        f"    print({stream!r})\n"
        "    raise SystemExit(7)\n",
    )
    runner = AgyAgentRunner(
        _write_config(tmp_path, str(fake)), tmp_path / "logs"
    )

    result = runner._invoke(
        task_id="EXIT",
        phase="implement",
        prompt="test",
        workspace=tmp_path,
        model="gemini-low",
        effort="low",
        mode="accept-edits",
        settings=_settings(),
        timeout_minutes=1,
        log_name="exit.log",
    )

    assert not result.ok
    assert result.error == "Agent exit code 7"


def test_agy_writes_one_user_event_and_logs_version_once(tmp_path: Path) -> None:
    fake = make_python_script(
        tmp_path / "fake-agy-stdin.py",
        "import json, sys\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    with Path('version-count.txt').open('a', encoding='utf-8') as fh:\n"
        "        fh.write('1')\n"
        "    print('agy fake-1.0')\n"
        "else:\n"
        "    lines = sys.stdin.read().splitlines()\n"
        "    message = json.loads(lines[0])\n"
        "    print(json.dumps({'event': 'result', 'result': {\n"
        "        'status': 'SUCCESS',\n"
        "        'response': json.dumps({'count': len(lines), 'message': message}),\n"
        "        'usage': {},\n"
        "    }}))\n",
    )
    runner = AgyAgentRunner(
        _write_config(tmp_path, str(fake)), tmp_path / "logs"
    )

    results = [
        runner._invoke(
            task_id="STDIN",
            phase="implement",
            prompt="long prompt\nwith unicode λ",
            workspace=tmp_path,
            model="gemini-low",
            effort="low",
            mode="accept-edits",
            settings=_settings(),
            timeout_minutes=1,
            log_name=f"stdin-{index}.log",
        )
        for index in range(2)
    ]

    payload = json.loads(results[0].output)
    assert payload == {
        "count": 1,
        "message": {
            "event": "user",
            "message": {"content": "long prompt\nwith unicode λ"},
        },
    }
    assert all(result.ok for result in results)
    assert (tmp_path / "logs" / "agy-version.log").read_text(
        encoding="utf-8"
    ) == "agy fake-1.0\n"
    assert (tmp_path / "version-count.txt").read_text(encoding="utf-8") == "1"


def _git_init(workspace: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)


def test_agy_watchdog_stalls_silent_idle_agent(tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    _git_init(workspace)
    fake = make_python_script(
        tmp_path / "fake-agy-idle.py",
        "import sys, time\n"
        "if '--version' in sys.argv:\n"
        "    print('agy fake-1.0')\n"
        "else:\n"
        "    _ = sys.stdin.read()\n"
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

    result = runner._invoke(
        task_id="IDLE",
        phase="implement",
        prompt="test",
        workspace=workspace,
        model="gemini-low",
        effort="low",
        mode="accept-edits",
        settings=_settings(),
        timeout_minutes=1,
        log_name="idle.log",
    )

    assert not result.ok
    assert result.stalled
    assert result.waiting_cause == "timeout"


def test_agy_watchdog_allows_silent_agent_with_file_activity(tmp_path: Path) -> None:
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    _git_init(workspace)
    stream = _result_stream()
    fake = make_python_script(
        tmp_path / "fake-agy-active.py",
        "import sys, time\n"
        "from pathlib import Path\n"
        "if '--version' in sys.argv:\n"
        "    print('agy fake-1.0')\n"
        "else:\n"
        "    _ = sys.stdin.read()\n"
        "    for i in range(50):\n"
        "        with (Path.cwd() / 'progress.txt').open('a', encoding='utf-8') as fh:\n"
        "            fh.write(str(i))\n"
        "        time.sleep(0.1)\n"
        f"    print({stream!r})\n",
    )
    runner = AgyAgentRunner(
        _write_config(
            tmp_path,
            str(fake),
            stall_timeout_minutes=0.05,
            watchdog_poll_seconds=0.02,
        ),
        tmp_path / "logs",
    )

    result = runner._invoke(
        task_id="ACTIVE",
        phase="implement",
        prompt="test",
        workspace=workspace,
        model="gemini-low",
        effort="low",
        mode="accept-edits",
        settings=_settings(),
        timeout_minutes=1,
        log_name="active.log",
    )

    assert result.ok
    assert not result.stalled
    assert (workspace / "progress.txt").exists()


def test_implement_and_review_build_phase_settings_and_prompts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = AgyAgentRunner(_write_config(tmp_path), tmp_path / "logs")
    task = _task(tmp_path)
    calls: list[dict[str, object]] = []

    def fake_invoke(**kwargs: object):
        calls.append(kwargs)
        from harness.runner import AgentResult

        return AgentResult(True, "ok", provider="agy")

    monkeypatch.setattr(runner, "_invoke", fake_invoke)
    monkeypatch.setattr(
        runner, "_diff_stat", lambda _workspace, _base: " agy.py | 2 ++"
    )
    runner.implement(task, tmp_path, 1, 1, None, base_sha="abc123")
    runner.review(task, tmp_path, "def456", "passed", 1)

    implementation, review = calls
    assert implementation["mode"] == "accept-edits"
    assert "command(python -m pytest -q tests/harness)" in str(
        implementation["settings"]
    )
    assert "command(git diff abc123)" in str(implementation["settings"])
    guidance = (
        "Use agy's own file tools to list, read, and search files instead of shell "
        "commands. The base commit, changed files, and diff summary are already in "
        "the prompt. Do not run `graphify`."
    )
    assert guidance in str(implementation["prompt"])
    assert review["mode"] == "plan"
    assert "command(python -m pytest -q tests/harness)" not in str(review["settings"])
    assert "command(git diff def456)" in str(review["settings"])
    assert guidance in str(review["prompt"])
    assert "Diff summary (`git diff --stat def456`):" in str(review["prompt"])
    assert "agy.py | 2 ++" in str(review["prompt"])
