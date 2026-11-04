from __future__ import annotations

import time
from pathlib import Path

import yaml

from harness.config import HarnessConfig
from harness.headroom import Headroom
from harness.models import TaskSpec
from harness.provider_runner import ProviderAgentRunner, clear_task_quota_disables
from harness.runner import AgentResult
from harness.state import StateStore


class FakeCodex:
    def __init__(self, log_dir: Path) -> None:
        self.log_dir = log_dir
        self.implement_calls = 0
        self.review_calls = 0

    def availability(self, **_kwargs):
        return True, "ok"

    def implement(self, *_args, **_kwargs):
        self.implement_calls += 1
        return AgentResult(True, "ok", provider="codex")

    def review(self, *_args, **_kwargs):
        self.review_calls += 1
        return AgentResult(True, "VERDICT: PASS", provider="codex")


class FakeCursor:
    def implement(self, *_args, **_kwargs):
        return AgentResult(True, "ok", provider="cursor")

    def review(self, *_args, **_kwargs):
        return AgentResult(True, "VERDICT: PASS", provider="cursor")


class FakeAgy:
    def __init__(self, *, quota: bool = False) -> None:
        self.quota = quota
        self.implement_calls: list[tuple[int, set[str]]] = []
        self.review_calls = 0

    def implement(self, _task, _workspace, _timeout, attempt, _previous, **kwargs):
        exhausted = set(kwargs.get("exhausted_pools", ()))
        self.implement_calls.append((attempt, exhausted))
        if self.quota:
            return AgentResult(
                False, "", "Quota exhausted", provider="agy",
                quota_exhausted=True, quota_scope="gemini-weekly",
            )
        return AgentResult(True, "ok", provider="agy")

    def review(self, *_args, **_kwargs):
        self.review_calls += 1
        return AgentResult(True, "VERDICT: PASS", provider="agy")


def config(tmp_path: Path) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "agy": {
                    "enabled": True,
                    "implementation_sequence": [
                        {"model": "gemini-3.8-flash-medium"},
                        {"model": "claude-opus-4-6-thinking", "effort": "high"},
                    ],
                },
                "codex": {
                    "enabled": True,
                    "implementation_sequence": [
                        {"model": "codex-model", "reasoning_effort": "medium"}
                    ],
                },
                "cursor": {"enabled": True, "models": {"worker": "cursor-model"}},
                "claude": {"enabled": False},
                "providers": {"priority": ["agy", "codex", "cursor", "claude"]},
                "retry": {"sequence": ["worker"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return HarnessConfig.load(path)


def task(tmp_path: Path) -> TaskSpec:
    brief = tmp_path / "task.md"
    brief.write_text("# task\n", encoding="utf-8")
    return TaskSpec(
        id="T1", file=brief, sprint="S1", stage=1, review=True,
        model_class="worker", allowed_paths=["**"],
    )


def snapshot(gemini: float, third_party: float) -> dict[str, list[Headroom]]:
    reset = int(time.time()) + 3600
    return {
        "agy": [
            Headroom("agy", gemini, reset, "weekly", True),
            Headroom("agy", third_party, reset + 86400, "weekly", True),
        ],
        "codex": [Headroom("codex", 99, reset + 86400, "five_hour", True)],
        "cursor": [Headroom("cursor", 99, reset + 86400, "other_models", True)],
    }


def router(tmp_path: Path, headroom, agy: FakeAgy):
    state = StateStore(tmp_path / "state.json")
    codex = FakeCodex(tmp_path / "logs")
    value = ProviderAgentRunner(
        config(tmp_path), state, codex, FakeCursor(), claude=None, agy=agy,
        headroom_reader=lambda: headroom,
    )
    return value, state, codex


def test_agy_uses_urgent_gemini_pool_and_falls_through_to_free_3p_pool(tmp_path: Path):
    first_agy = FakeAgy()
    first, _state, codex = router(tmp_path, snapshot(20, 20), first_agy)
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    assert first.implement(task(tmp_path), worktree, 1, 1, None).provider == "agy"
    assert first_agy.implement_calls == [(1, set())]
    assert codex.implement_calls == 0

    other_path = tmp_path / "other"
    other_path.mkdir()
    second_agy = FakeAgy()
    second, _state, codex = router(other_path, snapshot(100, 20), second_agy)
    other_worktree = other_path / "worktree"
    other_worktree.mkdir()

    assert second.implement(task(other_path), other_worktree, 1, 1, None).provider == "agy"
    assert second_agy.implement_calls == [(1, {"gemini-weekly"})]
    assert codex.implement_calls == 0


def test_agy_implementer_never_reviews_when_another_provider_is_available(tmp_path: Path):
    agy = FakeAgy()
    value, _state, codex = router(tmp_path, snapshot(20, 20), agy)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    spec = task(tmp_path)

    assert value.implement(spec, worktree, 1, 1, None).provider == "agy"
    assert value.review(spec, worktree, "base", "ok", 1).provider == "codex"
    assert agy.review_calls == 0
    assert codex.review_calls == 1


def test_agy_quota_failure_disables_and_falls_back_then_reset_clears(tmp_path: Path):
    agy = FakeAgy(quota=True)
    value, state, codex = router(tmp_path, snapshot(20, 20), agy)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    spec = task(tmp_path)

    result = value.implement(spec, worktree, 1, 1, None)

    assert result.provider == "codex"
    assert codex.implement_calls == 1
    key = "S1.T1.provider.agy.disabled"
    assert state.get_meta(key) == "quota:gemini-weekly"
    clear_task_quota_disables(state, "T1")
    assert state.get_meta(key) is None

    state.set_meta(key, "auth")
    clear_task_quota_disables(state, "T1")
    assert state.get_meta(key) is None
