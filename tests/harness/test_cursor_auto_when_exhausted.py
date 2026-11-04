from __future__ import annotations

import time
from pathlib import Path

import pytest
import yaml

from harness.config import HarnessConfig
from harness.headroom import Headroom
from harness.models import TaskSpec
from harness.provider_runner import ProviderAgentRunner
from harness.runner import AgentResult
from harness.state import StateStore

MODELS = {
    "worker": "composer-2.5",
    "hard_worker": "cursor-grok-4.6-high",
    "reviewer": "cursor-grok-4.6-high",
    "escalation": "claude-opus-5-thinking-high",
}


def _config(tmp_path: Path, *, auto: bool = True, claude: bool = False) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(
        yaml.safe_dump({
            "cursor": {"enabled": True, "models": MODELS, "auto_when_exhausted": auto},
            "codex": {
                "enabled": True,
                "implementation_sequence": [{"model": "m", "reasoning_effort": "medium"}],
            },
            "claude": {"enabled": claude},
            "providers": {"priority": ["codex", "cursor"]},
            "retry": {"sequence": ["worker", "hard_worker"]},
        }),
        encoding="utf-8",
    )
    return HarnessConfig.load(path)


class _Codex:
    log_dir = Path.cwd() / ".test-logs"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def availability(self, **_kw):
        return True, "ok"

    def implement(self, *_a, **_kw):
        self.calls.append("implement")
        return AgentResult(True, "ok", provider="codex")

    def review(self, *_a, **_kw):
        self.calls.append("review")
        return AgentResult(True, "VERDICT: PASS\n", provider="codex")


class _Cursor:
    def __init__(self, config: HarnessConfig) -> None:
        self.config = config
        self.models: list[tuple[str, str | None]] = []

    def implement(self, *_a, **kw):
        cls = kw.get("model_class_override")
        model = "auto" if cls == "auto" else self.config.cursor.models.get(cls)
        self.models.append(("implement", model))
        return AgentResult(True, "ok", provider="cursor")

    def review(self, *_a, **_kw):
        self.models.append(("review", self.config.cursor.models.get("reviewer")))
        return AgentResult(True, "VERDICT: PASS\n", provider="cursor")

    def plan_change(self, *_a, **kw):
        self.models.append(("plan_repair", kw.get("model_class")))
        return AgentResult(True, "plan", provider="cursor")


class _Claude:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def implement(self, *_a, **_kw):
        self.calls.append("implement")
        return AgentResult(True, "ok", provider="claude")

    def review(self, *_a, **_kw):
        self.calls.append("review")
        return AgentResult(True, "VERDICT: PASS\n", provider="claude")


def _snap(composer: float | None, other: float | None, codex: float = 10):
    now = int(time.time()) + 3600 * 100

    def entry(window: str, used: float | None) -> Headroom:
        if used is None:
            return Headroom("cursor", None, None, window, False)
        return Headroom("cursor", used, now, window, True)

    return {
        "cursor": [entry("composer_grok", composer), entry("other_models", other)],
        # Codex has plenty of headroom unless quota-disabled (then 100%); Claude is
        # out of capacity, so with Claude routing on it cannot take the call.
        "codex": [
            Headroom("codex", codex, int(time.time()) + 3600, "five_hour", True),
            Headroom("codex", codex, int(time.time()) + 3600, "weekly", True),
        ],
        "claude": [
            Headroom("claude", 100, now, "five_hour", True),
            Headroom("claude", 100, now, "weekly", True),
        ],
    }


def _task(tmp_path: Path) -> TaskSpec:
    brief = tmp_path / "t.md"
    brief.write_text("# t\n", encoding="utf-8")
    return TaskSpec(
        id="T1", file=brief, sprint="S1", stage=1, review=True,
        model_class="worker", allowed_paths=["**"],
    )


def _setup(
    tmp_path: Path, composer, other, *, auto: bool = True, codex_off: bool = False,
    claude: bool = False,
):
    config = _config(tmp_path, auto=auto, claude=claude)
    state = StateStore(tmp_path / "state.json")
    codex, cursor = _Codex(), _Cursor(config)
    codex.log_dir = tmp_path / "logs"
    runner = ProviderAgentRunner(
        config, state, codex, cursor, None,  # type: ignore[arg-type]
        _Claude() if claude else None,  # type: ignore[arg-type]
        headroom_reader=lambda: _snap(composer, other, 100 if codex_off else 10),
    )
    task = _task(tmp_path)
    state.get(task.id)
    if codex_off:
        state.set_meta(f"{task.sprint}.{task.id}.provider.codex.disabled", "quota")
    return runner, codex, cursor, task


def _implement(runner, task, tmp_path):
    return runner.implement(task, tmp_path, 1, 1, None)


def _review(runner, task, tmp_path):
    return runner.review(task, tmp_path, "HEAD", "ok", 1)


def _routing_log(root: Path, task: TaskSpec) -> str:
    return (root / "logs" / f"{task.id}-routing.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("claude", [False, True])
def test_both_pools_full_implement_runs_cursor_auto(tmp_path, claude):
    runner, codex, cursor, task = _setup(tmp_path, 100, 100, claude=claude)
    _implement(runner, task, tmp_path)
    assert cursor.models == [("implement", "auto")]
    assert codex.calls == []


@pytest.mark.parametrize("claude", [False, True])
def test_both_pools_full_review_does_not_use_cursor(tmp_path, claude):
    runner, codex, cursor, task = _setup(tmp_path, 100, 100, claude=claude)
    _review(runner, task, tmp_path)
    assert cursor.models == []
    assert codex.calls == ["review"]


@pytest.mark.parametrize("claude", [False, True])
def test_both_pools_full_review_waits_without_other_provider(tmp_path, claude):
    runner, codex, cursor, task = _setup(tmp_path, 100, 100, codex_off=True, claude=claude)
    result = _review(runner, task, tmp_path)
    assert cursor.models == []
    assert not result.ok


@pytest.mark.parametrize("pools", [(100, 40), (40, 100), (100, None)])
def test_one_full_pool_uses_codex_and_then_waits(tmp_path, pools):
    runner, codex, cursor, task = _setup(tmp_path, *pools, claude=True)
    _implement(runner, task, tmp_path)
    assert codex.calls == ["implement"] and cursor.models == []

    other = tmp_path / "b"
    other.mkdir()
    runner, codex, cursor, task = _setup(
        other, *pools, codex_off=True, claude=True
    )
    result = _implement(runner, task, other)
    assert cursor.models == [] and not result.ok


def test_unknown_cursor_usage_uses_codex_then_named_cursor_model(tmp_path):
    runner, codex, cursor, task = _setup(tmp_path, None, None, claude=True)
    _implement(runner, task, tmp_path)
    assert codex.calls == ["implement"] and cursor.models == []

    other = tmp_path / "b"
    other.mkdir()
    runner, codex, cursor, task = _setup(
        other, None, None, codex_off=True, claude=True
    )
    _implement(runner, task, other)
    assert cursor.models == [("implement", "composer-2.5")]


@pytest.mark.parametrize("claude", [False, True])
def test_pools_not_full_review_on_cursor_with_named_model(tmp_path, claude):
    runner, codex, cursor, task = _setup(tmp_path, 40, 40, codex_off=True, claude=claude)
    _review(runner, task, tmp_path)
    assert cursor.models == [("review", "cursor-grok-4.6-high")]


def test_flag_off_keeps_today_behavior(tmp_path):
    runner, codex, cursor, task = _setup(tmp_path, 100, 100, auto=False)
    _implement(runner, task, tmp_path)
    assert codex.calls == ["implement"] and cursor.models == []
    assert "auto" not in runner.config.cursor.models


def test_flag_off_with_claude_routing_keeps_today_capacity_rule(tmp_path):
    runner, _codex, cursor, task = _setup(
        tmp_path, 100, 40, auto=False, codex_off=True, claude=True
    )
    result = _implement(runner, task, tmp_path)
    # Today's rule: any Cursor window at 100% means Cursor is out.
    assert cursor.models == [] and not result.ok


def test_not_exhausted_flag_does_not_change_routing_at_codex_sequence_end(tmp_path):
    # With Cursor not exhausted, the flag changes nothing: budget and provider
    # match the flag-off path (routing moves on to Cursor once Codex is spent).
    outcomes = []
    for auto in (False, True):
        root = tmp_path / str(auto)
        root.mkdir()
        runner, _codex, _cursor, task = _setup(root, 40, 40, auto=auto)
        runner.state.get(task.id).provider_attempts["codex"] = 1
        outcomes.append((runner.has_implementation_budget(task), runner.active_provider(task)))
    assert outcomes[0] == outcomes[1] == (True, "codex")


def test_routing_log_names_auto_or_named_model(tmp_path, capsys):
    runner, _codex, _cursor, task = _setup(tmp_path, 100, 100)
    _implement(runner, task, tmp_path)
    assert "cursor: auto (both pools at 100%)" in _routing_log(tmp_path, task)

    other = tmp_path / "b"
    other.mkdir()
    runner, _codex, _cursor, task = _setup(other, 40, 40, codex_off=True, claude=True)
    capsys.readouterr()
    _review(runner, task, other)
    _implement(runner, task, other)
    out = capsys.readouterr().out
    assert "provider choice for review: cursor" in out
    assert "cursor: cursor-grok-4.6-high" in out
    assert "cursor: composer-2.5" in out


def test_plan_repair_log_does_not_claim_auto(tmp_path):
    # Plan repair runs the planner's model_class, so the implementation-only
    # model note (named model or auto) must not be appended to its log line.
    runner, _codex, cursor, task = _setup(tmp_path, 40, 40, codex_off=True, claude=True)
    runner.plan_change(task, tmp_path, "failed", 1, model_class="escalation")
    assert cursor.models == [("plan_repair", "escalation")]
    log = _routing_log(tmp_path, task)
    assert "provider choice for plan_repair: cursor" in log
    assert "; cursor:" not in log


def test_repo_harness_yaml_uses_named_models_with_auto_when_exhausted():
    path = Path(__file__).resolve().parents[2] / "harness.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = HarnessConfig.load(path)
    assert config.cursor.auto_when_exhausted is True
    assert raw["cursor"]["models"] == MODELS
    assert config.cursor.models == MODELS
