from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import harness.outcomes as outcomes
from harness.usage import OUTCOME_KEYS, UsageRecorder


def _enable_shared(tmp_home: Path, monkeypatch) -> Path:
    folder = tmp_home / ".harness"
    monkeypatch.setattr(outcomes, "_folder_configured", True)
    monkeypatch.setattr(outcomes, "_folder_override", folder)
    return folder


def _write_records(folder: str, project: str, prefix: str, count: int) -> None:
    outcomes.set_outcomes_folder(Path(folder))
    recorder = UsageRecorder(Path(project) / ".harness" / "usage.jsonl")
    for index in range(count):
        recorder.record({"task_id": f"{prefix}-{index}", "input_tokens": index})


def test_parallel_projects_append_complete_json_lines(tmp_path: Path) -> None:
    count = 40
    shared = tmp_path / "home" / ".harness"
    contexts = multiprocessing.get_context("spawn")
    processes = [
        contexts.Process(
            target=_write_records,
            args=(str(shared), str(tmp_path / name), name, count),
        )
        for name in ("project-a", "project-b")
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0

    lines = (shared / "outcomes.jsonl").read_text(encoding="utf-8").splitlines()
    records = [json.loads(line) for line in lines]
    assert len(records) == count * 2
    assert len({record["task_id"] for record in records}) == count * 2
    assert {record["project"] for record in records} == {
        str((tmp_path / "project-a").resolve()),
        str((tmp_path / "project-b").resolve()),
    }


def test_shared_record_has_complete_nullable_schema(tmp_path: Path, monkeypatch) -> None:
    shared = _enable_shared(tmp_path / "home", monkeypatch)
    local_path = tmp_path / "project" / ".harness" / "usage.jsonl"

    UsageRecorder(local_path).record({"task_id": "S9-T6", "ok": True})

    record = json.loads((shared / "outcomes.jsonl").read_text(encoding="utf-8"))
    local = json.loads(local_path.read_text(encoding="utf-8"))
    assert set(record) == set(OUTCOME_KEYS)
    assert record["recorded_at"] == local["recorded_at"]
    assert record["task_id"] == "S9-T6"
    assert record["ok"] is True
    assert record["model"] is None
    assert record["verification_ok"] is None


def test_known_outcome_fields_copied_from_usage_record(
    tmp_path: Path, monkeypatch
) -> None:
    """Values present on the usage event must appear in the shared log (not forced null)."""
    shared = _enable_shared(tmp_path / "home", monkeypatch)
    local_path = tmp_path / "project" / ".harness" / "usage.jsonl"
    known = {
        "sprint": "S9",
        "task_id": "S9-T6",
        "model_class": "worker",
        "provider": "codex",
        "model": "gpt-5.6-sol",
        "effort": "high",
        "phase": "review",
        "attempt": 2,
        "ok": True,
        "verification_ok": False,
        "review_verdict": "FAIL",
        "finding_count": 3,
        "input_tokens": 100,
        "cached_input_tokens": 10,
        "output_tokens": 50,
        "reasoning_output_tokens": 5,
        "duration_seconds": 12.5,
    }

    UsageRecorder(local_path).record(known)

    record = json.loads((shared / "outcomes.jsonl").read_text(encoding="utf-8"))
    local = json.loads(local_path.read_text(encoding="utf-8"))
    assert set(record) == set(OUTCOME_KEYS)
    for key, value in known.items():
        assert record[key] == value
        assert local[key] == value
    assert record["project"] == str((tmp_path / "project").resolve())
    assert record["recorded_at"] == local["recorded_at"]


def test_unwritable_shared_folder_does_not_fail_usage_record(
    tmp_path: Path, monkeypatch, caplog
) -> None:
    local_path = tmp_path / "project" / ".harness" / "usage.jsonl"
    blocked = tmp_path / "home"
    blocked.write_text("not a directory", encoding="utf-8")
    monkeypatch.setattr(outcomes, "_folder_configured", True)
    monkeypatch.setattr(outcomes, "_folder_override", blocked / ".harness")
    monkeypatch.setattr(outcomes, "_warning_logged", False)
    recorder = UsageRecorder(local_path)

    recorder.record({"task_id": "one"})
    recorder.record({"task_id": "two"})

    assert len(recorder.events()) == 2
    assert caplog.text.count("Shared outcomes log unavailable") == 1


def test_usage_record_skips_shared_log_under_pytest_by_default(
    tmp_path: Path, monkeypatch
) -> None:
    """Harness tests that call record() must not touch the machine-wide log."""
    sentinel_home = tmp_path / "sentinel-home"
    monkeypatch.setattr(outcomes, "_folder_configured", False)
    monkeypatch.setattr(
        Path, "home", classmethod(lambda cls: sentinel_home)
    )
    local_path = tmp_path / "project" / ".harness" / "usage.jsonl"

    UsageRecorder(local_path).record({"task_id": "no-poison"})

    assert len(UsageRecorder(local_path).events()) == 1
    assert not (sentinel_home / ".harness").exists()
