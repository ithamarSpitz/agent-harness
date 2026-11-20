from __future__ import annotations

import time
from pathlib import Path

import yaml

from harness.cli import build_runtime
from harness.config import HarnessConfig
from harness.headroom import Headroom
from harness.kiro_runner import KiroAgentRunner
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
        self.plan_calls = 0
        self.brief_calls = 0

    def availability(self, **_kwargs):
        return True, "ok"

    def implement(self, *_args, **_kwargs):
        self.implement_calls += 1
        if self.quota:
            return AgentResult(
                False,
                "",
                "Kiro quota exhausted",
                provider=self.provider,
                quota_exhausted=True,
                quota_scope="monthly",
            )
        return AgentResult(True, "ok", provider=self.provider)

    def review(self, *_args, **_kwargs):
        self.review_calls += 1
        return AgentResult(True, "VERDICT: PASS", provider=self.provider)

    def plan_change(self, *_args, **_kwargs):
        self.plan_calls += 1
        return AgentResult(True, "plan", provider=self.provider)

    def brief_check(self, *_args, **_kwargs):
        self.brief_calls += 1
        return AgentResult(True, "BRIEF OK", provider=self.provider)


def _config(tmp_path: Path) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "kiro": {
                    "enabled": True,
                    "implementation_sequence": ["auto"],
                },
                "codex": {
                    "enabled": True,
                    "implementation_sequence": [{"model": "codex"}],
                },
                "cursor": {"enabled": False},
                "providers": {"priority": ["kiro", "codex"]},
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
        id="T1",
        file=brief,
        sprint="S1",
        stage=1,
        review=True,
        model_class="worker",
        allowed_paths=["**"],
    )


def test_build_runtime_constructs_kiro_only_when_enabled(tmp_path: Path) -> None:
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "T1.md").write_text(
        "---\nid: T1\nmodel_class: worker\nallowed_paths: ['**']\n---\n# task\n",
        encoding="utf-8",
    )
    roadmap = tasks / "roadmap.yaml"
    roadmap.write_text(
        "project: test\nsprints:\n  S1:\n    tasks:\n"
        "      T1: {file: tasks/T1.md, stage: 1, depends_on: []}\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "harness.yaml"

    config_path.write_text("kiro:\n  enabled: false\n", encoding="utf-8")
    disabled = build_runtime(tmp_path, roadmap, config_path)
    assert disabled.runner.kiro is None

    config_path.write_text("kiro:\n  enabled: true\n", encoding="utf-8")
    enabled = build_runtime(tmp_path, roadmap, config_path)
    assert isinstance(enabled.runner.kiro, KiroAgentRunner)


def _router(tmp_path: Path, kiro: FakeProvider, codex: FakeProvider):
    state = StateStore(tmp_path / "state.json")
    reset = int(time.time()) + 3600
    headroom = {
        "kiro": [Headroom("kiro", 10, reset, "monthly", True)],
        "codex": [Headroom("codex", 10, reset, "weekly", True)],
    }
    return (
        ProviderAgentRunner(
            _config(tmp_path),
            state,
            codex,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            kiro=kiro,  # type: ignore[arg-type]
            headroom_reader=lambda: headroom,
        ),
        state,
    )


def test_kiro_implements_and_another_provider_reviews(tmp_path: Path) -> None:
    kiro, codex = FakeProvider("kiro"), FakeProvider("codex")
    router, _state = _router(tmp_path, kiro, codex)
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    assert router.implement(task, workspace, 1, 1, None).provider == "kiro"
    assert router.review(task, workspace, "base", "ok", 1).provider == "codex"
    assert (kiro.implement_calls, kiro.review_calls) == (1, 0)
    assert codex.review_calls == 1


def test_kiro_quota_failure_disables_it_and_falls_back(tmp_path: Path) -> None:
    kiro, codex = FakeProvider("kiro", quota=True), FakeProvider("codex")
    router, state = _router(tmp_path, kiro, codex)
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    result = router.implement(task, workspace, 1, 1, None)

    assert result.ok and result.provider == "codex"
    assert codex.implement_calls == 1
    assert state.get_meta("S1.T1.provider.kiro.disabled") == "quota:monthly"


def test_kiro_routes_plan_repair_and_brief_check(tmp_path: Path) -> None:
    kiro, codex = FakeProvider("kiro"), FakeProvider("codex")
    router, _state = _router(tmp_path, kiro, codex)
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    plan = router.plan_change(
        task, workspace, "failed", 1, model_class="planner"
    )
    brief = router.brief_check(task, workspace, "brief", 1)

    assert plan.ok and plan.provider == "kiro"
    assert brief.ok and brief.provider == "kiro"
    assert (kiro.plan_calls, kiro.brief_calls) == (1, 1)
