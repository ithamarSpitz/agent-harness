from __future__ import annotations

import json
from pathlib import Path

import harness.outcomes as outcomes
from harness.cli import main
from harness.outcomes_report import render_models_report


def _write_log(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )
    return path


def _call(
    *,
    project: str,
    task_id: str,
    provider: str,
    model: str,
    effort: str | None,
    phase: str,
    attempt: int,
    recorded_at: str,
    ok: bool = True,
    verification_ok: bool | None = None,
    review_verdict: str | None = None,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> dict:
    return {
        "project": project,
        "task_id": task_id,
        "provider": provider,
        "model": model,
        "effort": effort,
        "phase": phase,
        "attempt": attempt,
        "ok": ok,
        "verification_ok": verification_ok,
        "review_verdict": review_verdict,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "recorded_at": recorded_at,
    }


def _final(
    *,
    project: str,
    task_id: str,
    attempts: list[dict],
    passing: dict | None,
    status: str = "DONE",
    impl_attempts: int | None = None,
    total_tokens: int = 100,
    recorded_at: str = "2026-10-01T12:00:00+00:00",
) -> dict:
    return {
        "event_type": "task_final",
        "project": project,
        "task_id": task_id,
        "model_class": "worker",
        "final_status": status,
        "implementation_attempts": impl_attempts
        if impl_attempts is not None
        else len(attempts),
        "attempts": attempts,
        "passing_attempt": passing,
        "total_tokens": total_tokens,
        "recorded_at": recorded_at,
    }


def _sample_records(project_a: str, project_b: str) -> list[dict]:
    """Fixed sample: 14 sonnet/medium starts (3 first-pass, 11 fixed) plus misc rows."""
    records: list[dict] = []
    medium = {"provider": "cursor", "model": "sonnet", "effort": "medium"}
    high = {"provider": "cursor", "model": "sonnet", "effort": "high"}
    opus = {"provider": "cursor", "model": "opus", "effort": None}

    # 3 tasks that pass on first sonnet/medium attempt
    for index in range(3):
        task_id = f"PASS-{index}"
        records.append(
            _call(
                project=project_a,
                task_id=task_id,
                provider="cursor",
                model="sonnet",
                effort="medium",
                phase="implement",
                attempt=1,
                recorded_at="2026-10-05T10:00:00+00:00",
                verification_ok=True,
            )
        )
        records.append(
            _call(
                project=project_a,
                task_id=task_id,
                provider="cursor",
                model="sonnet",
                effort="medium",
                phase="review",
                attempt=1,
                recorded_at="2026-10-05T10:01:00+00:00",
                review_verdict="PASS",
            )
        )
        records.append(
            _final(
                project=project_a,
                task_id=task_id,
                attempts=[medium],
                passing=medium,
                impl_attempts=1,
                total_tokens=100,
                recorded_at="2026-10-05T10:02:00+00:00",
            )
        )

    # 8 fixed by sonnet/high
    for index in range(8):
        task_id = f"HIGH-{index}"
        records.append(
            _call(
                project=project_a,
                task_id=task_id,
                provider="cursor",
                model="sonnet",
                effort="medium",
                phase="implement",
                attempt=1,
                recorded_at="2026-10-06T10:00:00+00:00",
                ok=False,
                verification_ok=False,
            )
        )
        records.append(
            _call(
                project=project_a,
                task_id=task_id,
                provider="cursor",
                model="sonnet",
                effort="high",
                phase="implement",
                attempt=2,
                recorded_at="2026-10-06T11:00:00+00:00",
                verification_ok=True,
            )
        )
        records.append(
            _final(
                project=project_a,
                task_id=task_id,
                attempts=[medium, high],
                passing=high,
                impl_attempts=2,
                total_tokens=200,
                recorded_at="2026-10-06T11:02:00+00:00",
            )
        )

    # 3 fixed by opus
    for index in range(3):
        task_id = f"OPUS-{index}"
        records.append(
            _call(
                project=project_a,
                task_id=task_id,
                provider="cursor",
                model="sonnet",
                effort="medium",
                phase="implement",
                attempt=1,
                recorded_at="2026-10-07T10:00:00+00:00",
                ok=False,
                verification_ok=False,
            )
        )
        records.append(
            _call(
                project=project_a,
                task_id=task_id,
                provider="cursor",
                model="opus",
                effort=None,
                phase="implement",
                attempt=2,
                recorded_at="2026-10-07T11:00:00+00:00",
                verification_ok=True,
            )
        )
        records.append(
            _final(
                project=project_a,
                task_id=task_id,
                attempts=[medium, opus],
                passing=opus,
                impl_attempts=2,
                total_tokens=300,
                recorded_at="2026-10-07T11:02:00+00:00",
            )
        )

    # Older record in project_b (for --project / --since filters)
    records.append(
        _call(
            project=project_b,
            task_id="OLD-1",
            provider="codex",
            model="gpt-5.6-sol",
            effort="high",
            phase="implement",
            attempt=1,
            recorded_at="2026-09-01T09:00:00+00:00",
            verification_ok=True,
        )
    )
    records.append(
        _final(
            project=project_b,
            task_id="OLD-1",
            attempts=[{"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"}],
            passing={"provider": "codex", "model": "gpt-5.6-sol", "effort": "high"},
            impl_attempts=1,
            total_tokens=50,
            recorded_at="2026-09-01T09:05:00+00:00",
        )
    )

    # Call with missing model/effort (unknown bucket, must not error)
    records.append(
        _call(
            project=project_a,
            task_id="SPARSE",
            provider="cursor",
            model=None,  # type: ignore[arg-type]
            effort=None,
            phase="plan_repair",
            attempt=1,
            recorded_at="2026-10-08T12:00:00+00:00",
        )
    )
    return records


def test_fixed_sample_produces_table_and_suggestion(tmp_path: Path) -> None:
    project_a = str((tmp_path / "proj-a").resolve())
    project_b = str((tmp_path / "proj-b").resolve())
    log_path = _write_log(
        tmp_path / "outcomes.jsonl", _sample_records(project_a, project_b)
    )

    text = render_models_report(log_path)

    assert "provider" in text and "first_pass" in text
    assert "sonnet" in text and "medium" in text and "implement" in text
    # 14 implement calls on sonnet/medium attempt paths + reviews etc.
    assert "3/14" in text
    assert "11/14" in text
    # avg_att = (3*1 + 11*2) / 14 ≈ 1.8; tokens/done = 2800/14 = 200
    medium_lines = [
        line
        for line in text.splitlines()
        if "sonnet" in line and "medium" in line and "implement" in line
    ]
    assert medium_lines, text
    assert "1.8" in medium_lines[0]
    assert "200" in medium_lines[0]
    assert "suggestions:" in text
    assert (
        "sonnet/medium: 3 of 14 first attempts passed; "
        "11 were fixed by sonnet/high or opus. "
        "Consider starting with sonnet/high."
    ) in text
    # Unknown model from sparse record
    assert "unknown" in text
    # Codex row from project_b present in unfiltered report
    assert "gpt-5.6-sol" in text


def test_project_and_since_filters(tmp_path: Path) -> None:
    project_a = str((tmp_path / "proj-a").resolve())
    project_b = str((tmp_path / "proj-b").resolve())
    log_path = _write_log(
        tmp_path / "outcomes.jsonl", _sample_records(project_a, project_b)
    )

    only_b = render_models_report(log_path, project=Path(project_b))
    assert "gpt-5.6-sol" in only_b
    assert "sonnet" not in only_b

    since_oct = render_models_report(log_path, since="2026-10-01")
    assert "sonnet" in since_oct
    assert "gpt-5.6-sol" not in since_oct

    since_nov = render_models_report(log_path, since="2026-11-01")
    assert since_nov.strip() == "No matching outcomes."


def test_empty_or_missing_log_prints_one_line(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "outcomes.jsonl"
    assert render_models_report(missing).strip() == "No outcomes recorded yet."

    empty = tmp_path / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    assert render_models_report(empty).strip() == "No outcomes recorded yet."


def test_cli_models_report_reads_shared_log(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    shared = tmp_path / "home" / ".harness"
    project_a = str((tmp_path / "proj-a").resolve())
    project_b = str((tmp_path / "proj-b").resolve())
    _write_log(shared / "outcomes.jsonl", _sample_records(project_a, project_b))
    monkeypatch.setattr(outcomes, "_folder_configured", True)
    monkeypatch.setattr(outcomes, "_folder_override", shared)

    assert main(["models", "report"]) == 0
    out = capsys.readouterr().out
    assert "suggestions:" in out
    assert "sonnet/medium" in out

    assert main(["models", "report", "--project", project_b]) == 0
    filtered = capsys.readouterr().out
    assert "gpt-5.6-sol" in filtered
    assert "sonnet" not in filtered

    assert main(["models", "report", "--since", "2026-11-01"]) == 0
    assert capsys.readouterr().out.strip() == "No matching outcomes."


def test_same_model_retry_is_not_first_attempt_pass(tmp_path: Path) -> None:
    """DONE after attempt 2+ on the same model/effort is not a first-attempt pass."""
    project = str((tmp_path / "proj").resolve())
    medium = {"provider": "cursor", "model": "sonnet", "effort": "medium"}
    records = [
        _call(
            project=project,
            task_id="RETRY-1",
            provider="cursor",
            model="sonnet",
            effort="medium",
            phase="implement",
            attempt=1,
            recorded_at="2026-10-05T10:00:00+00:00",
            ok=False,
            verification_ok=False,
        ),
        _call(
            project=project,
            task_id="RETRY-1",
            provider="cursor",
            model="sonnet",
            effort="medium",
            phase="implement",
            attempt=2,
            recorded_at="2026-10-05T11:00:00+00:00",
            verification_ok=True,
        ),
        _final(
            project=project,
            task_id="RETRY-1",
            attempts=[medium, medium],
            passing=medium,
            impl_attempts=2,
            total_tokens=150,
            recorded_at="2026-10-05T11:02:00+00:00",
        ),
    ]
    log_path = _write_log(tmp_path / "outcomes.jsonl", records)
    text = render_models_report(log_path)
    lines = [
        line
        for line in text.splitlines()
        if "sonnet" in line and "medium" in line and "implement" in line
    ]
    assert lines, text
    # first_pass 0/1; fixed 0/1 (same model is not "fixed by other")
    assert "0/1" in lines[0]
    assert lines[0].count("0/1") >= 2
    # avg_att = 2.0, tokens/done = 150
    assert "2.0" in lines[0]
    assert "150" in lines[0]
    assert "suggestions:" not in text


def test_null_passing_attempt_not_counted_as_fixed(tmp_path: Path) -> None:
    project = str((tmp_path / "proj").resolve())
    medium = {"provider": "cursor", "model": "sonnet", "effort": "medium"}
    records = [
        _call(
            project=project,
            task_id="NULL-PASS",
            provider="cursor",
            model="sonnet",
            effort="medium",
            phase="implement",
            attempt=1,
            recorded_at="2026-10-05T10:00:00+00:00",
            verification_ok=True,
        ),
        _final(
            project=project,
            task_id="NULL-PASS",
            attempts=[medium],
            passing=None,
            impl_attempts=1,
            total_tokens=80,
            recorded_at="2026-10-05T10:02:00+00:00",
        ),
    ]
    log_path = _write_log(tmp_path / "outcomes.jsonl", records)
    text = render_models_report(log_path)
    lines = [
        line
        for line in text.splitlines()
        if "sonnet" in line and "medium" in line and "implement" in line
    ]
    assert lines, text
    # first_pass 1/1 (DONE on attempt 1); fixed 0/1 (null passing is unknown)
    assert "1/1" in lines[0]
    assert "0/1" in lines[0]
    assert "suggestions:" not in text
    # Must not invent an "unknown" fixer row from null passing_attempt
    assert "fixed by unknown" not in text


def test_unknown_event_type_skipped_in_mixed_log(tmp_path: Path) -> None:
    project = str((tmp_path / "proj").resolve())
    medium = {"provider": "cursor", "model": "sonnet", "effort": "medium"}
    records = [
        _call(
            project=project,
            task_id="MIX-1",
            provider="cursor",
            model="sonnet",
            effort="medium",
            phase="implement",
            attempt=1,
            recorded_at="2026-10-05T10:00:00+00:00",
            verification_ok=True,
        ),
        {
            "event_type": "future_metric",
            "project": project,
            "task_id": "MIX-1",
            "provider": "cursor",
            "model": "sonnet",
            "effort": "medium",
            "phase": "implement",
            "recorded_at": "2026-10-05T10:01:00+00:00",
        },
        {
            "event_type": "provider_switch",
            "project": project,
            "provider": "codex",
            "switch_to": "cursor",
            "recorded_at": "2026-10-05T10:01:30+00:00",
        },
        _final(
            project=project,
            task_id="MIX-1",
            attempts=[medium],
            passing=medium,
            impl_attempts=1,
            total_tokens=40,
            recorded_at="2026-10-05T10:02:00+00:00",
        ),
    ]
    log_path = _write_log(tmp_path / "outcomes.jsonl", records)
    text = render_models_report(log_path)
    lines = [line for line in text.splitlines() if "sonnet" in line and "medium" in line]
    assert lines, text
    # Only the real call record counts (calls=1), unknown event_type ignored
    assert lines[0].split()[4] == "1"
