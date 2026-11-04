from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import yaml

from harness.board import render_board, snapshot_from_state
from harness.cli import cmd_reset_task, cmd_retry, cmd_run_task, cmd_unblock
from harness.config import HarnessConfig
from harness.headroom import Headroom, headroom_snapshot
from harness.limits import LimitsPaths
from harness.models import SprintSpec, TaskSpec, TaskStatus
from harness.provider_runner import ProviderAgentRunner, clear_task_quota_disables
from harness.routing import adapt_headroom_snapshot, choose_provider
from harness.runner import AgentResult
from harness.scheduler import TaskOutcome
from harness.state import StateStore
from harness.usage import UsageRecorder


def _config(tmp_path: Path, *, claude: bool = True) -> HarnessConfig:
    path = tmp_path / "harness.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "cursor": {"enabled": True, "models": {"worker": "cursor-model"}},
                "codex": {
                    "enabled": True,
                    "implementation_sequence": [
                        {"model": "codex-model", "reasoning_effort": "medium"}
                    ],
                },
                "claude": {
                    "enabled": claude,
                    "implementation_sequence": [{"model": "sonnet", "effort": "medium"}],
                    "reserve": {"min_percent": 60, "max_percent": 90},
                },
                "providers": {"priority": ["codex", "cursor", "claude"]},
                "retry": {"sequence": ["worker"]},
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


class _Codex:
    def __init__(self) -> None:
        self.log_dir = Path.cwd() / ".test-logs"
        self.implement_calls = 0
        self.review_calls = 0

    def availability(self, **_kwargs):
        return True, "ok"

    def implement(self, *_args, **_kwargs):
        self.implement_calls += 1
        return AgentResult(True, "ok", provider="codex")

    def review(self, *_args, **_kwargs):
        self.review_calls += 1
        return AgentResult(True, "VERDICT: PASS\n", provider="codex")


class _Cursor:
    def __init__(self) -> None:
        self.implement_calls = 0
        self.review_calls = 0
        self.last_model_class: str | None = None

    def implement(self, *_args, **kwargs):
        self.implement_calls += 1
        self.last_model_class = kwargs.get("model_class_override")
        return AgentResult(True, "ok", provider="cursor")

    def review(self, *_args, **_kwargs):
        self.review_calls += 1
        return AgentResult(True, "VERDICT: PASS\n", provider="cursor")


class _Claude:
    def __init__(self) -> None:
        self.implement_calls = 0
        self.review_calls = 0

    def implement(self, *_args, **_kwargs):
        self.implement_calls += 1
        return AgentResult(True, "ok", provider="claude")

    def review(self, *_args, **_kwargs):
        self.review_calls += 1
        return AgentResult(True, "VERDICT: PASS\n", provider="claude")


def _headroom(*, claude_used: float = 20, codex_used: float = 10) -> dict[str, list[Headroom]]:
    now = int(time.time())
    return {
        "codex": [Headroom("codex", codex_used, now + 100 * 3600, "weekly", True)],
        "cursor": [Headroom("cursor", 95, now + 100 * 3600, "other_models", True)],
        "claude": [
            Headroom("claude", claude_used, now + 3600, "five_hour", True),
            Headroom("claude", 20, now + 7 * 24 * 3600, "weekly", True),
        ],
    }


def _router(tmp_path: Path, snapshot, *, claude_enabled: bool = True):
    config = _config(tmp_path, claude=claude_enabled)
    state = StateStore(tmp_path / "state.json")
    usage = UsageRecorder(tmp_path / "usage.jsonl")
    codex, cursor, claude = _Codex(), _Cursor(), _Claude()
    codex.log_dir = tmp_path / "logs"
    return (
        ProviderAgentRunner(
            config, state, codex, cursor, usage,
            claude if claude_enabled else None,
            headroom_reader=lambda: snapshot,
        ),
        state,
        usage,
        codex,
        cursor,
        claude,
    )


def test_headroom_favoring_claude_routes_implementation_and_logs_reason(tmp_path: Path):
    router, _state, usage, codex, cursor, claude = _router(tmp_path, _headroom())
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    result = router.implement(task, workspace, 1, 1, None)

    assert result.ok and result.provider == "claude"
    assert (codex.implement_calls, cursor.implement_calls, claude.implement_calls) == (0, 0, 1)
    # Routing choices go to the run log only, never to usage.jsonl.
    assert [e for e in usage.events() if e.get("routing_reason")] == []
    log = (tmp_path / "logs" / "T1-routing.log").read_text(encoding="utf-8")
    assert "provider choice for implement: claude (urgent: waste 50% of claude 5-hour" in log
    # The board reads the reason from this meta key.
    reason = _state.get_meta(f"{task.sprint}.{task.id}.board.routing_reason")
    assert isinstance(reason, str) and reason.startswith("urgent: waste 50% of claude 5-hour")

    # harness.board shows that reason (from the meta key, not usage.jsonl).
    runtime = _state.get(task.id)
    runtime.status = TaskStatus.IMPLEMENTING
    runtime.attempt = 1
    sprint = SprintSpec(id=task.sprint, name="Routing", tasks={task.id: task})
    snapshot = snapshot_from_state(_state, sprint, datetime.now(timezone.utc))
    assert snapshot.stages[0].tasks[0].routing_reason == reason
    lines = render_board(snapshot, 200)
    assert any("urgent: waste 50% of claude 5-hour" in line for line in lines)


def test_review_after_claude_implementation_uses_another_provider(tmp_path: Path):
    router, _state, _usage, codex, _cursor, claude = _router(tmp_path, _headroom())
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    assert router.implement(task, workspace, 1, 1, None).provider == "claude"
    review = router.review(task, workspace, "base", "ok", 1)

    assert review.provider == "codex"
    assert codex.review_calls == 1
    assert claude.review_calls == 0


def test_review_does_not_treat_task_auto_class_as_auto_only(tmp_path: Path):
    router, state, _usage, codex, cursor, claude = _router(tmp_path, _headroom())
    task = _task(tmp_path)
    task.model_class = "auto"
    workspace = tmp_path / "worktree"
    workspace.mkdir()
    state.set_meta("S1.T1.provider.implementer", "claude")

    review = router.review(task, workspace, "base", "ok", 1)

    assert review.provider == "codex"
    assert codex.review_calls == 1
    assert cursor.review_calls == 0
    assert claude.review_calls == 0


def test_claude_over_reserve_is_not_called(tmp_path: Path):
    router, _state, _usage, codex, _cursor, claude = _router(
        tmp_path, _headroom(claude_used=65)
    )
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    assert router.implement(task, workspace, 1, 1, None).provider == "codex"
    assert codex.implement_calls == 1
    assert claude.implement_calls == 0


def test_claude_disabled_still_routes_by_the_rules_not_only_priority(tmp_path: Path):
    # Codex has 1% left but resets in 100h (no waste); Cursor's pool resets in 1h
    # with 5% left... its waste is 5 - 1 x 1 = 4%, so Cursor goes first.
    now = int(time.time())
    snapshot = {
        "codex": [Headroom("codex", 99, now + 100 * 3600, "weekly", True)],
        "cursor": [Headroom("cursor", 95, now + 3600, "other_models", True)],
    }
    router, _state, _usage, codex, cursor, _claude = _router(
        tmp_path, snapshot, claude_enabled=False
    )
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    assert router.implement(task, workspace, 1, 1, None).provider == "cursor"
    assert (codex.implement_calls, cursor.implement_calls) == (0, 1)


def test_no_headroom_data_follows_priority_exactly_in_every_project(tmp_path: Path):
    router, _state, _usage, codex, cursor, _claude = _router(
        tmp_path, {}, claude_enabled=False
    )
    router.config.providers.priority = ["cursor", "codex"]
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    assert router.implement(task, workspace, 1, 1, None).provider == "cursor"
    assert (codex.implement_calls, cursor.implement_calls) == (0, 1)


def test_review_never_runs_on_the_implementer_when_another_has_usage(tmp_path: Path):
    router, state, _usage, codex, cursor, _claude = _router(
        tmp_path, {}, claude_enabled=False
    )
    task = _task(tmp_path)
    workspace = tmp_path / "worktree"
    workspace.mkdir()

    assert router.implement(task, workspace, 1, 1, None).provider == "codex"
    assert state.get_meta("S1.T1.provider.implementer") == "codex"
    review = router.review(task, workspace, "base", "ok", 1)

    assert review.provider == "cursor"
    assert (codex.review_calls, cursor.review_calls) == (0, 1)


def test_real_headroom_result_adapts_for_choose_provider(tmp_path: Path):
    now = int(time.time())

    def source(payload):
        return lambda _paths: ([], payload)

    snapshot = headroom_snapshot(
        max_age_seconds=0,
        paths=LimitsPaths(
            codex_home=tmp_path / "codex",
            claude_usage_cache=tmp_path / "claude" / "usage-cache.json",
            cursor_state_db=tmp_path / "cursor.db",
            claude_home=tmp_path / "claude",
        ),
        cache_path=tmp_path / "headroom.json",
        codex_source=source({
            "primary": {"used_percent": 10, "resets_at": now + 100 * 3600},
            "secondary": {"used_percent": 10, "resets_at": now + 100 * 3600},
        }),
        cursor_source=source({
            "billing_cycle_end_ms": (now + 100 * 3600) * 1000,
            "auto_percent_used": 95,
            "api_percent_used": 95,
        }),
        claude_source=source({"windows": {
            "five_hour": {"used_percentage": 20, "resets_at": now + 3600},
            "seven_day": {"used_percentage": 20, "resets_at": now + 7 * 86400},
        }}),
    )

    choice = choose_provider(
        adapt_headroom_snapshot(snapshot), phase="implement", model_class="worker",
        enabled=("codex", "cursor", "claude"),
        priority=("codex", "cursor", "claude"), implementer_provider=None,
        disabled_for_task=set(), claude_reserve=_config(tmp_path).claude.reserve,
        now=datetime.now(timezone.utc),
    )

    assert choice.provider == "claude"


def test_quota_disable_clears_when_snapshot_has_capacity_claude_off(tmp_path: Path):
    """Stale quota:* must clear from headroom even without Claude routing.

    Matches the Nov 3 WAITING_TIMEOUT case where Codex sat disabled after reset.
    """
    router, state, _usage, _codex, _cursor, _claude = _router(
        tmp_path, _headroom(codex_used=0), claude_enabled=False
    )
    task = _task(tmp_path)
    key = "S1.T1.provider.codex.disabled"
    runtime = state.get(task.id)
    runtime.status = TaskStatus.WAITING_TIMEOUT
    state.set_meta(key, "quota:5h")
    state.save()

    # If the disable were left in place, priority would skip Codex → Cursor.
    assert router.active_provider(task) == "codex"
    assert state.get_meta(key) is None


def test_quota_disable_clears_when_snapshot_has_capacity_claude_on(tmp_path: Path):
    # Claude over reserve so ranking prefers Codex; a lingering disable would
    # skip Codex and pick Cursor instead.
    router, state, _usage, _codex, _cursor, _claude = _router(
        tmp_path, _headroom(claude_used=65, codex_used=0)
    )
    task = _task(tmp_path)
    key = "S1.T1.provider.codex.disabled"
    state.set_meta(key, "quota:5h")
    state.save()

    assert router.active_provider(task) == "codex"
    assert state.get_meta(key) is None


def test_waiting_quota_resume_clears_quota_disable_claude_on(tmp_path: Path, capsys):
    """Claude path must clear quota:* on WAITING_QUOTA even without free capacity."""
    # Codex still shows 100% so capacity-based clear alone would leave the disable.
    router, state, _usage, _codex, _cursor, _claude = _router(
        tmp_path, _headroom(claude_used=65, codex_used=100)
    )
    task = _task(tmp_path)
    key = "S1.T1.provider.codex.disabled"
    state.set_meta(key, "quota:5h")
    runtime = state.get(task.id)
    runtime.status = TaskStatus.WAITING_QUOTA
    runtime.waiting_phase = "implement"
    runtime.waiting_provider = "cursor"
    runtime.last_error = "cursor usage quota exhausted during implement"
    state.save()

    assert router.has_implementation_budget(task)
    assert state.get_meta(key) is None
    assert "codex re-enabled for T1 (was: quota:5h)" in capsys.readouterr().out


def test_manual_reset_commands_clear_quota_disables(tmp_path: Path):
    state = StateStore(tmp_path / "state.json")
    task_id = "T1"
    key = "S1.T1.provider.codex.disabled"

    state.get(task_id)
    state.set_meta(key, "quota:5h")
    assert cmd_retry(state, task_id) == 0
    assert state.get_meta(key) is None

    state.get(task_id).status = TaskStatus.BLOCKED
    state.set_meta(key, "quota:weekly")
    assert cmd_unblock(state, task_id) == 0
    assert state.get_meta(key) is None

    task = TaskSpec(
        id=task_id, file=tmp_path / "task.md", sprint="S1", stage=1,
        model_class="worker", allowed_paths=["**"],
    )
    (tmp_path / "task.md").write_text("# task\n", encoding="utf-8")
    state.set_meta(key, "quota:5h")
    reset_runtime = SimpleNamespace(
        scheduler=SimpleNamespace(
            task_queue=SimpleNamespace(resolve_task=lambda roadmap, tid: task),
        ),
        roadmap=object(),
        state=state,
        worktrees=SimpleNamespace(
            remove=lambda *a, **k: None,
            delete_branch=lambda *a, **k: None,
        ),
        failures=SimpleNamespace(resolve_task=lambda *a, **k: None),
    )
    runtime = state.get(task_id)
    runtime.status = TaskStatus.BLOCKED
    runtime.worktree = None
    runtime.branch = None
    state.save()
    assert cmd_reset_task(reset_runtime, task_id) == 0
    assert state.get_meta(key) is None

    state.set_meta(key, "quota:5h")
    task_runtime = state.get(task_id)
    task_runtime.status = TaskStatus.RETRY
    task_runtime.base_ref = "base"
    state.save()

    def run_task(task_arg, base_commit, *, model_class_override):
        return TaskOutcome(task_id=task_id, ok=True)

    run_runtime = SimpleNamespace(
        roadmap=object(),
        scheduler=SimpleNamespace(
            task_queue=SimpleNamespace(resolve_task=lambda roadmap, tid: task),
            _run_task=run_task,
        ),
        validator=SimpleNamespace(validate=lambda sprint_id: SimpleNamespace(ok=True)),
        config=SimpleNamespace(
            cursor=SimpleNamespace(models={"worker": "m"}),
            execution=SimpleNamespace(keep_awake_during_run=False),
        ),
        state=state,
        worktrees=SimpleNamespace(repo_root=tmp_path),
        failures=SimpleNamespace(resolve_task=lambda *a, **k: None),
    )
    assert cmd_run_task(run_runtime, task_id, fresh_budget=True) == 0
    assert state.get_meta(key) is None


def test_clear_task_quota_disables_helper(tmp_path: Path):
    state = StateStore(tmp_path / "state.json")
    key = "S1.T1.provider.codex.disabled"
    state.set_meta(key, "quota:weekly")
    clear_task_quota_disables(state, "T1")
    assert state.get_meta(key) is None
