from __future__ import annotations

import time
from pathlib import Path

import yaml

from harness.config import HarnessConfig
from harness.headroom import Headroom
from harness.models import TaskSpec
from harness.provider_runner import ProviderAgentRunner
from harness.runner import AgentResult
from harness.state import StateStore


class FakeProvider:
    def __init__(self, provider: str, *, quota: bool = False) -> None:
        self.provider = provider
        self.quota = quota
        self.implement_calls = 0
        self.review_calls = 0

    def availability(self, **_kwargs):
        return True, "ok"

    def implement(self, *_args, **_kwargs):
        self.implement_calls += 1
        if self.quota:
            return AgentResult(
                False, "", "Copilot premium quota exhausted",
                provider=self.provider, quota_exhausted=True, quota_scope="premium",
            )
        return AgentResult(True, "ok", provider=self.provider)

    def review(self, *_args, **_kwargs):
        self.review_calls += 1
        return AgentResult(True, "VERDICT: PASS", provider=self.provider)


def _config(tmp_path: Path) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "copilot": {
                    "enabled": True,
                    "implementation_sequence": [{"model": "auto"}],
                },
                "codex": {
                    "enabled": True,
                    "implementation_sequence": [{"model": "codex"}],
                },
                "cursor": {"enabled": False},
                "providers": {"priority": ["copilot", "codex"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return HarnessConfig.load(path)


def _task(tmp_path: Path) -> TaskSpec:
    brief = tmp_path / "task.md"
    brief.write_text("# task\n", encoding="utf-8")
    return TaskSpec(
        id="T1", file=brief, sprint="S1", stage=1, review=True,
        model_class="worker", allowed_paths=["**"],
    )


def _router(tmp_path: Path, copilot: FakeProvider, codex: FakeProvider):
    state = StateStore(tmp_path / "state.json")
    reset = int(time.time()) + 3600
    headroom = {
        "copilot": [Headroom("copilot", 10, reset, "premium", True)],
        "codex": [Headroom("codex", 10, reset, "weekly", True)],
    }
    return (
        ProviderAgentRunner(
            _config(tmp_path), state, codex, None, copilot=copilot,
            headroom_reader=lambda: headroom,
        ),
        state,
    )


def test_copilot_implements_and_codex_reviews(tmp_path: Path) -> None:
    copilot, codex = FakeProvider("copilot"), FakeProvider("codex")
    router, _state = _router(tmp_path, copilot, codex)
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    assert router.implement(task, workspace, 1, 1, None).provider == "copilot"
    assert router.review(task, workspace, "base", "ok", 1).provider == "codex"
    assert (copilot.implement_calls, copilot.review_calls) == (1, 0)
    assert codex.review_calls == 1


def test_copilot_reviews_its_own_work_when_it_is_only_provider_with_usage(
    tmp_path: Path,
) -> None:
    copilot, codex = FakeProvider("copilot"), FakeProvider("codex")
    router, _state = _router(tmp_path, copilot, codex)
    router.config.codex.enabled = False
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    assert router.implement(task, workspace, 1, 1, None).provider == "copilot"
    assert router.review(task, workspace, "base", "ok", 1).provider == "copilot"
    assert copilot.review_calls == 1


def test_copilot_quota_failure_falls_back_to_codex(tmp_path: Path) -> None:
    copilot, codex = FakeProvider("copilot", quota=True), FakeProvider("codex")
    router, state = _router(tmp_path, copilot, codex)
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    result = router.implement(task, workspace, 1, 1, None)

    assert result.provider == "codex"
    assert codex.implement_calls == 1
    assert state.get_meta("S1.T1.provider.copilot.disabled") == "quota:premium"
