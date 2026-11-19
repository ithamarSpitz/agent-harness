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

    def brief_check(self, *_args, **_kwargs):
        self.brief_calls = getattr(self, "brief_calls", 0) + 1
        return AgentResult(True, "BRIEF OK", provider=self.provider)


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


class ScriptedCopilot(FakeProvider):
    def __init__(self, *, implement_faults: int, review_fault: bool = False) -> None:
        super().__init__("copilot")
        self.implement_faults = implement_faults
        self.review_fault = review_fault

    def _fault(self) -> AgentResult:
        return AgentResult(
            False, "", 'Error: Model "auto" does not support reasoning effort',
            provider="copilot", config_fault=True,
        )

    def implement(self, *_args, **_kwargs):
        self.implement_calls += 1
        if self.implement_calls <= self.implement_faults:
            return self._fault()
        return AgentResult(True, "ok", provider="copilot")

    def review(self, *_args, **_kwargs):
        self.review_calls += 1
        if self.review_fault:
            return self._fault()
        return AgentResult(True, "VERDICT: PASS", provider="copilot")


def _two_step_router(tmp_path: Path, copilot: FakeProvider, codex: FakeProvider):
    router, state = _router(tmp_path, copilot, codex)
    router.config.copilot.implementation_sequence = (
        router.config.copilot.implementation_sequence * 2
    )
    return router, state


def test_config_fault_advances_step_and_keeps_copilot(tmp_path: Path) -> None:
    copilot, codex = ScriptedCopilot(implement_faults=1), FakeProvider("codex")
    router, state = _two_step_router(tmp_path, copilot, codex)
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    result = router.implement(task, workspace, 1, 1, None)

    assert result.ok and result.provider == "copilot"
    assert copilot.implement_calls == 2
    assert codex.implement_calls == 0
    assert state.get_meta("S1.T1.provider.copilot.disabled") is None
    assert router._provider_attempt(task, "copilot") == 2


def test_all_config_faults_fall_through_without_disabling(tmp_path: Path) -> None:
    copilot, codex = ScriptedCopilot(implement_faults=99), FakeProvider("codex")
    router, state = _two_step_router(tmp_path, copilot, codex)
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    result = router.implement(task, workspace, 1, 1, None)

    assert result.provider == "codex" and result.ok
    assert copilot.implement_calls == 2
    assert state.get_meta("S1.T1.provider.copilot.disabled") is None


def test_review_config_fault_uses_other_provider(tmp_path: Path) -> None:
    copilot = ScriptedCopilot(implement_faults=0, review_fault=True)
    codex = FakeProvider("codex")
    router, state = _router(tmp_path, copilot, codex)
    router.config.codex.enabled = True
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    # Make Copilot the preferred reviewer by putting codex out of the way first.
    result = router._review_with_routing(
        task, workspace, "base", "ok", 1, env=None,
        code_map_offered=False, on_process_start=None,
    )

    assert result.ok
    assert result.provider in {"codex", "copilot"}
    assert state.get_meta("S1.T1.provider.copilot.disabled") is None
    assert router.implement(task, workspace, 1, 1, None).provider == "copilot"


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


def _unavailable(fake: FakeProvider) -> None:
    fake.availability = lambda **_kwargs: (False, "down")  # type: ignore[method-assign]


def test_brief_check_copilot_unavailable_falls_to_codex(tmp_path: Path) -> None:
    copilot, codex = FakeProvider("copilot"), FakeProvider("codex")
    _unavailable(copilot)
    router, state = _router(tmp_path, copilot, codex)
    result = router.brief_check(_task(tmp_path), tmp_path, "brief", 5)
    assert result.ok and result.provider == "codex"
    assert getattr(copilot, "brief_calls", 0) == 0
    assert state.get_meta("S1.T1.provider.copilot.disabled") == "auth_or_cli_unavailable"


def test_brief_check_both_unavailable(tmp_path: Path) -> None:
    copilot, codex = FakeProvider("copilot"), FakeProvider("codex")
    _unavailable(copilot)
    _unavailable(codex)
    router, state = _router(tmp_path, copilot, codex)
    result = router.brief_check(_task(tmp_path), tmp_path, "brief", 5)
    assert (
        not result.ok
        and result.provider == "codex"
        and result.auth_failure
        and result.error == "Codex CLI/auth unavailable"
    )
    assert getattr(codex, "brief_calls", 0) == 0
    assert state.get_meta("S1.T1.provider.copilot.disabled") == "auth_or_cli_unavailable"
    assert state.get_meta("S1.T1.provider.codex.disabled") == "auth_or_cli_unavailable"


def test_brief_check_copilot_runner_auth_failure_keeps_auth_reason(tmp_path: Path) -> None:
    copilot, codex = FakeProvider("copilot"), FakeProvider("codex")
    copilot.brief_check = lambda *_a, **_k: AgentResult(  # type: ignore[method-assign]
        False, "", "auth expired", provider="copilot", auth_failure=True,
    )
    router, state = _router(tmp_path, copilot, codex)
    result = router.brief_check(_task(tmp_path), tmp_path, "brief", 5)
    assert result.ok and result.provider == "codex"
    assert state.get_meta("S1.T1.provider.copilot.disabled") == "auth"
