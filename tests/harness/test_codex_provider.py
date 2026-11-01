from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import yaml

from harness.codex_runner import CodexAgentRunner
from harness.config import HarnessConfig
from harness.models import TaskSpec
from harness.provider_runner import ProviderAgentRunner
from harness.process_utils import locate_codex_executable
from harness.runner import AgentResult
from harness.state import StateStore
from harness.usage import UsageRecorder


def _task(tmp_path: Path) -> TaskSpec:
    task_file = tmp_path / "task.md"
    task_file.write_text("# task\n", encoding="utf-8")
    return TaskSpec(id="T1", file=task_file, sprint="S1", stage=1, review=True, allowed_paths=["**"])


def _write_config(tmp_path: Path, codex_command: str) -> HarnessConfig:
    payload = {
        "execution": {
            "max_parallel_agents": 1,
            "default_max_attempts": 2,
            "default_timeout_minutes": 1,
            "stall_timeout_minutes": 5,
            "watchdog_poll_seconds": 0.1,
        },
        "cursor": {
            "enabled": True,
            "command": "agent",
            "models": {
                "worker": "composer-2.5",
                "hard_worker": "cursor-grok-4.6-high",
                "reviewer": "cursor-grok-4.6-high",
                "escalation": "claude-opus-5-thinking-high",
            },
        },
        "providers": {"priority": ["codex", "cursor"]},
        "codex": {
            "enabled": True,
            "command": codex_command,
            "reasoning_effort": "high",
            "sandbox": "workspace-write",
            "windows_implementation_sandbox": "danger-full-access",
            "implementation_sequence": [
                {"model": "gpt-5.6-sol", "reasoning_effort": "medium"},
                {"model": "gpt-5.6-sol", "reasoning_effort": "medium"},
                {"model": "gpt-5.6-sol", "reasoning_effort": "high"},
                {"model": "gpt-6-astra", "reasoning_effort": "medium"},
            ],
            "review_model": "gpt-5.6-sol",
            "planner_model": "gpt-5.6-sol",
        },
        "retry": {"sequence": ["worker", "worker", "hard_worker", "escalation"]},
        "verification": {"global_task_commands": ["git diff --check"], "stage_commands": []},
        "paths": {"protected": [], "review_required": []},
    }
    path = tmp_path / "harness.yaml"
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return HarnessConfig.load(path)


def test_codex_implementation_profiles_are_exact_and_legacy_strings_inherit_default(tmp_path: Path):
    config = _write_config(tmp_path, "codex")
    assert [config.codex.implementation_profile(i) for i in range(4)] == [
        ("gpt-5.6-sol", "medium"),
        ("gpt-5.6-sol", "medium"),
        ("gpt-5.6-sol", "high"),
        ("gpt-6-astra", "medium"),
    ]
    config.codex.implementation_sequence = ["legacy-model"]
    assert config.codex.implementation_profile(0) == ("legacy-model", "high")


def test_cursor_fallback_ladder_is_unchanged(tmp_path: Path):
    config = _write_config(tmp_path, "codex")
    assert config.retry.sequence == ["worker", "worker", "hard_worker", "escalation"]
    assert config.cursor.models == {
        "worker": "composer-2.5",
        "hard_worker": "cursor-grok-4.6-high",
        "reviewer": "cursor-grok-4.6-high",
        "escalation": "claude-opus-5-thinking-high",
    }


def _fake_codex_cli(tmp_path: Path) -> Path:
    script = tmp_path / "fake-codex.py"
    script.write_text(
        r'''from __future__ import annotations
import json, os, sys, time
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("codex-cli 0.154.0")
    raise SystemExit(0)
if args == ["login", "status"]:
    print("Logged in using ChatGPT")
    raise SystemExit(0)
if not args or args[0] != "exec":
    print("unexpected args", args, file=sys.stderr)
    raise SystemExit(2)

prompt = sys.stdin.read()
workspace = Path(args[args.index("--cd") + 1])
model = args[args.index("--model") + 1]
(workspace / "fake-codex-argv.json").write_text(json.dumps(args), encoding="utf-8")
(workspace / "fake-codex-stdin.txt").write_text(prompt, encoding="utf-8")

if "FAKE_SLEEP" in prompt:
    time.sleep(30)
if "FAKE_QUOTA_5H" in prompt:
    print("5-hour usage limit exhausted", file=sys.stderr)
    raise SystemExit(1)
if "FAKE_QUOTA_WEEKLY" in prompt:
    print("weekly usage limit exhausted", file=sys.stderr)
    raise SystemExit(1)
if "FAKE_QUOTA_CURLY" in prompt:
    sys.stderr.buffer.write("You\u2019ve hit your usage limit. Try again at 1:29 AM.\n".encode("utf-8"))
    raise SystemExit(1)
if "FAKE_QUOTA" in prompt:
    print("You've hit your usage limit. Try again later.", file=sys.stderr)
    raise SystemExit(1)
if "FAKE_CAPACITY" in prompt:
    print("RetriableError: [resource_exhausted] Error", file=sys.stderr)
    raise SystemExit(1)
if "FAKE_TRANSIENT" in prompt:
    print("Connection reset by peer", file=sys.stderr)
    raise SystemExit(1)
if "FAKE_MODEL_UNAVAILABLE" in prompt:
    print("model is not available", file=sys.stderr)
    raise SystemExit(1)
if "FAKE_EDIT" in prompt:
    (workspace / "probe.txt").write_text("CODEX_HARNESS_OK\n", encoding="utf-8")

print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"PROBE PASS"}}))
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":11,"cached_input_tokens":3,"output_tokens":7,"reasoning_output_tokens":2}}))
''',
        encoding="utf-8",
    )
    return script


def test_codex_auth_sol_medium_stdin_cwd_edit_and_structured_usage(tmp_path: Path):
    cli = _fake_codex_cli(tmp_path)
    config = _write_config(tmp_path, str(cli))
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    runner = CodexAgentRunner(config, tmp_path / "logs", usage=usage)

    ok, detail = runner.availability(refresh=True)
    assert ok
    assert "Logged in using ChatGPT" in detail

    workspace = tmp_path / "worktree"
    workspace.mkdir()
    result = runner._invoke(
        task_id="SMOKE", phase="implement", prompt="FAKE_EDIT\ncheck cwd and tool execution",
        workspace=workspace, model="gpt-5.6-sol", reasoning_effort="medium", timeout_minutes=1,
        log_name="smoke.log", env=None,
    )

    assert result.ok
    assert result.provider == "codex"
    assert result.output == "PROBE PASS"
    assert result.input_tokens == 11
    assert result.cached_input_tokens == 3
    assert result.output_tokens == 7
    assert result.reasoning_output_tokens == 2
    assert (workspace / "probe.txt").read_text(encoding="utf-8") == "CODEX_HARNESS_OK\n"
    assert (workspace / "fake-codex-stdin.txt").read_text(encoding="utf-8").startswith("FAKE_EDIT")

    argv = json.loads((workspace / "fake-codex-argv.json").read_text(encoding="utf-8"))
    assert argv[0] == "exec"
    assert "--json" in argv
    assert "--ephemeral" in argv
    assert argv[argv.index("--model") + 1] == "gpt-5.6-sol"
    assert argv[argv.index("--cd") + 1] == str(workspace)
    assert 'model_reasoning_effort="medium"' in argv
    assert 'approval_policy="never"' in argv
    assert "FAKE_EDIT" not in " ".join(argv)  # prompt must stay off argv

    rows = usage.summary_by_provider()
    assert rows["codex:gpt-5.6-sol"]["calls"] == 1
    assert rows["codex:gpt-5.6-sol"]["input_tokens"] == 11


def test_codex_default_effort_remains_high_when_no_implementation_override_is_supplied(tmp_path: Path):
    cli = _fake_codex_cli(tmp_path)
    config = _write_config(tmp_path, str(cli))
    runner = CodexAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "default-effort"
    workspace.mkdir()

    result = runner._invoke(
        task_id="DEFAULT", phase="review", prompt="check default effort", workspace=workspace,
        model="gpt-5.6-sol", timeout_minutes=1, log_name="default.log", read_only=True,
    )

    assert result.ok
    argv = json.loads((workspace / "fake-codex-argv.json").read_text(encoding="utf-8"))
    assert 'model_reasoning_effort="high"' in argv


def test_codex_windows_implementation_uses_configured_sandbox_override(tmp_path: Path):
    config = _write_config(tmp_path, "codex")
    runner = CodexAgentRunner(config, tmp_path / "logs")

    assert runner._effective_sandbox(read_only=False, windows=True) == "danger-full-access"
    assert runner._effective_sandbox(read_only=False, windows=False) == "workspace-write"
    assert runner._effective_sandbox(read_only=True, windows=True) == "read-only"


def test_codex_timeout_cancels_without_becoming_substantive_failure(tmp_path: Path):
    cli = _fake_codex_cli(tmp_path)
    config = _write_config(tmp_path, str(cli))
    runner = CodexAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    started = time.monotonic()
    result = runner._invoke(
        task_id="TIMEOUT", phase="implement", prompt="FAKE_SLEEP", workspace=workspace,
        model="gpt-5.6-luna", timeout_minutes=0.01, log_name="timeout.log",
    )
    elapsed = time.monotonic() - started

    assert elapsed < 10
    assert not result.ok
    assert result.timed_out
    assert result.transient_error
    assert not result.quota_exhausted
    assert not result.capacity_exhausted


def test_codex_quota_capacity_transient_and_model_classification(tmp_path: Path):
    cli = _fake_codex_cli(tmp_path)
    config = _write_config(tmp_path, str(cli))
    runner = CodexAgentRunner(config, tmp_path / "logs")
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    def invoke(prompt: str) -> AgentResult:
        return runner._invoke(
            task_id="C", phase="implement", prompt=prompt, workspace=workspace,
            model="gpt-5.6-luna", timeout_minutes=1, log_name=f"{prompt}.log",
        )

    quota = invoke("FAKE_QUOTA")
    assert quota.quota_exhausted and quota.quota_scope == "unspecified"
    assert not quota.capacity_exhausted and not quota.transient_error

    short_quota = invoke("FAKE_QUOTA_5H")
    assert short_quota.quota_exhausted and short_quota.quota_scope == "5h"

    curly_quota = invoke("FAKE_QUOTA_CURLY")
    assert curly_quota.quota_exhausted and not curly_quota.capacity_exhausted

    weekly_quota = invoke("FAKE_QUOTA_WEEKLY")
    assert weekly_quota.quota_exhausted and weekly_quota.quota_scope == "weekly"

    capacity = invoke("FAKE_CAPACITY")
    assert capacity.capacity_exhausted and not capacity.quota_exhausted and not capacity.transient_error

    transient = invoke("FAKE_TRANSIENT")
    assert transient.transient_error and not transient.quota_exhausted and not transient.capacity_exhausted

    unavailable = invoke("FAKE_MODEL_UNAVAILABLE")
    assert unavailable.model_unavailable and not unavailable.quota_exhausted


class _FakeCodex:
    def __init__(self, results: list[AgentResult] | None = None, available: bool = True):
        self.results = list(results or [])
        self.available = available
        self.models: list[str] = []
        self.efforts: list[str | None] = []
        self.review_models: list[str] = []

    def availability(self, **kwargs):
        return self.available, "ok" if self.available else "not logged in"

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, *, model, reasoning_effort=None, env=None):
        self.models.append(model)
        self.efforts.append(reasoning_effort)
        if self.results:
            return self.results.pop(0)
        return AgentResult(True, "ok", model=model, provider="codex")

    def review(self, task, workspace, base_ref, verification_summary, timeout_minutes, *, model, env=None):
        self.review_models.append(model)
        if self.results:
            return self.results.pop(0)
        return AgentResult(True, "VERDICT: PASS\n", model=model, provider="codex")

    def plan_change(self, *args, model, **kwargs):
        return AgentResult(True, "CHANGE_KIND: NONE\n", model=model, provider="codex")


class _FakeCursor:
    def __init__(self, results: list[AgentResult] | None = None):
        self.results = list(results or [])
        self.implement_classes: list[str | None] = []
        self.review_calls = 0

    def implement(self, task, workspace, timeout_minutes, attempt, previous_failure, **kwargs):
        self.implement_classes.append(kwargs.get("model_class_override"))
        if self.results:
            return self.results.pop(0)
        return AgentResult(True, "ok", model="cursor", provider="cursor")

    def review(self, *args, **kwargs):
        self.review_calls += 1
        if self.results:
            return self.results.pop(0)
        return AgentResult(True, "VERDICT: PASS\n", model="cursor-review", provider="cursor")

    def plan_change(self, *args, **kwargs):
        return AgentResult(True, "CHANGE_KIND: NONE\n", provider="cursor")


def _router(tmp_path: Path, codex: _FakeCodex, cursor: _FakeCursor):
    config = _write_config(tmp_path, "codex")
    state = StateStore(tmp_path / "state.json")
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    return ProviderAgentRunner(config, state, codex, cursor, usage), state, usage


def test_codex_substantive_ladder_is_sol_medium_sol_medium_sol_high_astra_medium_then_block_budget(tmp_path: Path):
    codex = _FakeCodex()
    cursor = _FakeCursor()
    router, state, _ = _router(tmp_path, codex, cursor)
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    for attempt in range(1, 5):
        result = router.implement(task, workspace, 1, attempt, None)
        assert result.ok

    assert codex.models == ["gpt-5.6-sol", "gpt-5.6-sol", "gpt-5.6-sol", "gpt-6-astra"]
    assert codex.efforts == ["medium", "medium", "high", "medium"]
    assert state.get(task.id).provider_attempts == {"codex": 4}
    assert not router.has_implementation_budget(task)
    assert cursor.implement_classes == []


def test_disabled_cursor_never_receives_codex_quota_fallback(tmp_path: Path):
    codex = _FakeCodex([AgentResult(False, "", "limit", quota_exhausted=True, provider="codex")])
    cursor = _FakeCursor([AgentResult(True, "cursor should not run", provider="cursor")])
    router, state, _ = _router(tmp_path, codex, cursor)
    router.config.cursor.enabled = False
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    result = router.implement(task, workspace, 1, 1, None)

    assert not result.ok
    assert result.provider == "codex"
    assert result.quota_exhausted
    assert cursor.implement_classes == []
    assert state.get_meta("S1.T1.provider.codex.disabled") is None
    assert router.has_implementation_budget(task)


def test_disabled_cursor_rejects_manual_model_class_override(tmp_path: Path):
    router, _, _ = _router(tmp_path, _FakeCodex(), _FakeCursor())
    router.config.cursor.enabled = False
    task = _task(tmp_path)

    assert not router.has_implementation_budget(task, model_class_override="worker")
    try:
        router.active_provider(task, model_class_override="worker")
    except RuntimeError as exc:
        assert "Cursor is disabled" in str(exc)
    else:
        raise AssertionError("disabled Cursor must require explicit re-enable before --model-class")


def test_disabled_cursor_never_receives_review_fallback(tmp_path: Path):
    codex = _FakeCodex([AgentResult(False, "", "limit", quota_exhausted=True, provider="codex")])
    cursor = _FakeCursor([AgentResult(True, "VERDICT: PASS\n", provider="cursor")])
    router, _, _ = _router(tmp_path, codex, cursor)
    router.config.cursor.enabled = False
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    result = router.review(task, workspace, "base", "verification ok", 1)

    assert not result.ok
    assert result.provider == "codex"
    assert result.quota_exhausted
    assert cursor.review_calls == 0


def test_codex_quota_switches_to_cursor_same_attempt_and_preserves_task_attempt(tmp_path: Path):
    codex = _FakeCodex([AgentResult(False, "", "limit", quota_exhausted=True, provider="codex")])
    cursor = _FakeCursor([AgentResult(True, "cursor ok", provider="cursor")])
    router, state, usage = _router(tmp_path, codex, cursor)
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()
    state.get(task.id).attempt = 7
    state.save()

    result = router.implement(task, workspace, 1, 7, None)

    assert result.ok and result.provider == "cursor"
    assert codex.models == ["gpt-5.6-sol"]
    assert cursor.implement_classes == ["worker"]
    runtime = state.get(task.id)
    assert runtime.attempt == 7  # provider router never consumes scheduler attempt
    assert runtime.provider_attempts == {"cursor": 1}
    assert state.get_meta("S1.T1.provider.codex.disabled") == "quota:unspecified"
    switch_events = [e for e in usage.events() if e.get("event_type") == "provider_switch"]
    assert len(switch_events) == 1 and switch_events[0]["switch_to"] == "cursor"


def test_codex_quota_fallback_is_per_task_and_next_task_probes_codex_again(tmp_path: Path):
    codex = _FakeCodex([
        AgentResult(False, "", "limit", quota_exhausted=True, provider="codex"),
        AgentResult(True, "codex ok", provider="codex"),
    ])
    cursor = _FakeCursor([AgentResult(True, "cursor ok", provider="cursor")] * 2)
    router, state, _ = _router(tmp_path, codex, cursor)
    first = _task(tmp_path)
    second = TaskSpec(id="T2", file=first.file, sprint="S1", stage=1, review=True, allowed_paths=["**"])
    workspace = tmp_path / "w"
    workspace.mkdir()

    assert router.implement(first, workspace, 1, 1, None).provider == "cursor"
    assert router.implement(second, workspace, 1, 1, None).provider == "codex"
    # The task that fell back stays on Cursor for its own retries and review.
    assert router.active_provider(first) == "cursor"
    assert router.active_provider(second) == "codex"
    assert state.get_meta("S1.T2.provider.codex.disabled") is None


def test_codex_capacity_and_transient_retry_same_model_without_consuming_provider_attempt(tmp_path: Path):
    codex = _FakeCodex([
        AgentResult(False, "", "capacity", capacity_exhausted=True, provider="codex"),
        AgentResult(False, "", "network", transient_error=True, provider="codex"),
        AgentResult(True, "ok", provider="codex"),
    ])
    router, state, _ = _router(tmp_path, codex, _FakeCursor())
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    r1 = router.implement(task, workspace, 1, 1, None)
    r2 = router.implement(task, workspace, 1, 1, None)
    r3 = router.implement(task, workspace, 1, 1, None)

    assert r1.capacity_exhausted
    assert r2.transient_error
    assert r3.ok
    assert codex.models == ["gpt-5.6-sol", "gpt-5.6-sol", "gpt-5.6-sol"]
    assert codex.efforts == ["medium", "medium", "medium"]
    assert state.get(task.id).provider_attempts == {"codex": 1}
    assert state.get_meta("S1.T1.provider.codex.disabled") is None


def test_codex_review_quota_falls_back_to_cursor_without_reimplementation(tmp_path: Path):
    codex = _FakeCodex([AgentResult(False, "", "limit", quota_exhausted=True, provider="codex")])
    cursor = _FakeCursor([AgentResult(True, "VERDICT: PASS\n", provider="cursor")])
    router, state, _ = _router(tmp_path, codex, cursor)
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    result = router.review(task, workspace, "base", "verification ok", 1)

    assert result.ok
    assert codex.review_models == ["gpt-5.6-sol"]
    assert cursor.review_calls == 1
    assert cursor.implement_classes == []
    assert state.get(task.id).provider_attempts == {}
    assert state.get_meta("S1.T1.provider.codex.disabled") == "quota:unspecified"


def test_usage_summary_separates_codex_and_cursor(tmp_path: Path):
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    usage.record({"provider":"codex","model":"gpt-5.6-luna","ok":True,"duration_seconds":1.0,"input_tokens":10,"output_tokens":5})
    usage.record({"provider":"cursor","model":"composer-2.5","ok":True,"duration_seconds":2.0})
    usage.record({"event_type":"provider_switch","provider":"codex","switch_to":"cursor","ok":False,"duration_seconds":0.0})

    rows = usage.summary_by_provider()
    assert rows["codex:gpt-5.6-luna"]["calls"] == 1
    assert rows["cursor:composer-2.5"]["calls"] == 1
    assert rows["codex:provider_switch"]["provider_switches"] == 1

class _ExplodingCodex(_FakeCodex):
    def implement(self, *args, **kwargs):
        raise RuntimeError("adapter broke")


def test_codex_adapter_exception_falls_back_to_cursor_without_losing_state(tmp_path: Path):
    cursor = _FakeCursor([AgentResult(True, "cursor ok", provider="cursor")])
    router, state, _ = _router(tmp_path, _ExplodingCodex(), cursor)
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    result = router.implement(task, workspace, 1, 1, None)

    assert result.ok and result.provider == "cursor"
    assert cursor.implement_classes == ["worker"]
    assert state.get(task.id).provider_attempts == {"cursor": 1}
    assert str(state.get_meta("S1.T1.provider.codex.disabled")).startswith("integration_error:")


def test_provider_priority_can_explicitly_choose_cursor_first(tmp_path: Path):
    codex = _FakeCodex()
    cursor = _FakeCursor([AgentResult(True, "cursor ok", provider="cursor")])
    router, state, _ = _router(tmp_path, codex, cursor)
    router.config.providers.priority = ["cursor", "codex"]
    task = _task(tmp_path)
    workspace = tmp_path / "w"
    workspace.mkdir()

    result = router.implement(task, workspace, 1, 1, None)

    assert result.ok and result.provider == "cursor"
    assert codex.models == []
    assert cursor.implement_classes == ["worker"]
    assert state.get(task.id).provider_attempts == {"cursor": 1}


def test_codex_windows_standard_install_path_fallback_when_path_is_stale(tmp_path: Path):
    local = tmp_path / "LocalAppData"
    exe = local / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")

    resolved = locate_codex_executable(
        "codex",
        windows=True,
        localappdata=str(local),
        which=lambda _command: None,
    )

    assert resolved is not None
    assert Path(resolved) == exe.resolve()


def test_codex_explicit_path_still_wins_over_windows_fallback(tmp_path: Path):
    explicit = tmp_path / "custom-codex.exe"
    explicit.write_bytes(b"MZ")

    resolved = locate_codex_executable(str(explicit), windows=True, localappdata=str(tmp_path / "unused"))

    assert resolved == str(explicit.resolve())


def test_codex_probe_script_imports_harness_when_executed_directly(tmp_path: Path):
    root = Path(__file__).resolve().parents[2]
    probe = root / "scripts" / "probe-codex-provider.py"
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = subprocess.run(
        [sys.executable, str(probe), "--help"],
        cwd=tmp_path,
        env=env,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "--live" in result.stdout
