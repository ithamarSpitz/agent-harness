from __future__ import annotations

import json
from pathlib import Path

import harness.outcomes as outcomes
from harness.cli import main
from harness.outcomes_report import import_usage_records, load_outcome_records


def _write_usage(project: Path, records: list[dict]) -> None:
    usage = project / ".harness" / "usage.jsonl"
    usage.parent.mkdir(parents=True)
    usage.write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )


def test_imports_two_projects_once_and_preserves_sparse_records(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_usage(
        first,
        [
            {
                "task_id": "A",
                "provider": "cursor",
                "model": "composer",
                "phase": "implement",
                "attempt": 1,
                "recorded_at": "2026-01-01T10:00:00+00:00",
            },
            {
                "task_id": "A",
                "provider": "cursor",
                "model": "composer",
                "phase": "review",
                "attempt": 1,
                "recorded_at": "2026-01-01T10:01:00+00:00",
            },
        ],
    )
    _write_usage(
        second,
        [
            {
                "task_id": "B",
                "provider": "codex",
                "model": "gpt-test",
                "phase": "implement",
                "attempt": 1,
                "recorded_at": "2026-01-02T10:00:00+00:00",
            }
        ],
    )
    destination = tmp_path / "shared" / "outcomes.jsonl"

    assert import_usage_records([first, second], destination) == 3
    assert import_usage_records([first, second], destination) == 0

    records = load_outcome_records(destination)
    assert len(records) == 3
    assert {record["project"] for record in records} == {
        str(first.resolve()),
        str(second.resolve()),
    }
    assert all(record["imported"] is True for record in records)
    assert "effort" not in records[0]
    assert "verification_ok" not in records[0]


def test_cli_imported_records_are_counted_by_report(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    project = tmp_path / "project"
    _write_usage(
        project,
        [
            {
                "task_id": "A",
                "provider": "cursor",
                "model": "composer",
                "phase": "implement",
                "attempt": 1,
                "recorded_at": "2026-01-01T10:00:00+00:00",
            },
            {
                "task_id": "B",
                "provider": "cursor",
                "model": "composer",
                "phase": "implement",
                "attempt": 1,
                "recorded_at": "2026-01-02T10:00:00+00:00",
            },
        ],
    )
    shared = tmp_path / "home" / ".harness"
    monkeypatch.setattr(outcomes, "_folder_configured", True)
    monkeypatch.setattr(outcomes, "_folder_override", shared)

    assert main(["models", "import", str(project)]) == 0
    assert capsys.readouterr().out == "Imported 2 records.\n"
    assert main(["models", "import", str(project)]) == 0
    assert capsys.readouterr().out == "Imported 0 records.\n"

    assert main(["models", "report"]) == 0
    report = capsys.readouterr().out
    composer = next(line for line in report.splitlines() if "composer" in line)
    assert "implement" in composer
    assert "    2" in composer
