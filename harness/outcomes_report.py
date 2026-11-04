"""Aggregate ~/.harness/outcomes.jsonl into a models performance report."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any


UNKNOWN = "unknown"
_SUGGESTION_MIN_TASKS = 10


@dataclass(frozen=True, slots=True)
class ModelKey:
    provider: str
    model: str
    effort: str
    phase: str


@dataclass(slots=True)
class ModelRow:
    provider: str
    model: str
    effort: str
    phase: str
    calls: int
    first_attempt_passes: int
    first_attempt_total: int
    avg_attempts_to_done: float | None
    tokens_per_done: float | None
    fixed_by_other: int
    started_tasks: int


def default_outcomes_path() -> Path:
    """Path to the machine-wide outcomes log (honors outcomes.set_outcomes_folder)."""
    from . import outcomes as outcomes_mod
    from .home import harness_home

    if outcomes_mod._folder_configured:
        folder = outcomes_mod._folder_override
        if folder is not None:
            return folder / "outcomes.jsonl"
    return harness_home() / "outcomes.jsonl"


def load_outcome_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def _import_key(record: dict[str, Any]) -> tuple[Any, ...]:
    """Return the stable identity used when importing historical call records."""
    return (
        record.get("project"),
        record.get("task_id", record.get("task")),
        record.get("recorded_at"),
        record.get("phase"),
        record.get("attempt"),
    )


def import_usage_records(
    projects: list[Path], path: Path | None = None
) -> int:
    """Import each project's legacy usage log into the shared outcomes log once."""
    from .outcomes import _locked

    destination = path if path is not None else default_outcomes_path()
    candidates: list[dict[str, Any]] = []
    for project in projects:
        project_path = project.resolve()
        usage_path = project_path / ".harness" / "usage.jsonl"
        for record in load_outcome_records(usage_path):
            imported = dict(record)
            imported["project"] = str(project_path)
            imported["imported"] = True
            candidates.append(imported)

    if not candidates:
        return 0

    destination.parent.mkdir(parents=True, exist_ok=True)
    lock_path = destination.with_name("outcomes.lock")
    imported_count = 0
    with _locked(lock_path):
        known = {_import_key(record) for record in load_outcome_records(destination)}
        with destination.open("a", encoding="utf-8") as output:
            for record in candidates:
                key = _import_key(record)
                if key in known:
                    continue
                output.write(json.dumps(record, sort_keys=True) + "\n")
                known.add(key)
                imported_count += 1
    return imported_count


def _field(value: Any) -> str:
    if value is None or value == "":
        return UNKNOWN
    return str(value)


def _model_key(record: dict[str, Any]) -> ModelKey:
    return ModelKey(
        provider=_field(record.get("provider")),
        model=_field(record.get("model")),
        effort=_field(record.get("effort")),
        phase=_field(record.get("phase")),
    )


def _attempt_triplet(record: dict[str, Any] | None) -> tuple[str, str, str]:
    if not record:
        return (UNKNOWN, UNKNOWN, UNKNOWN)
    return (
        _field(record.get("provider")),
        _field(record.get("model")),
        _field(record.get("effort")),
    )


def _parse_recorded_date(value: Any) -> date | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).date()
    except ValueError:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None


def _project_matches(record_project: Any, wanted: Path) -> bool:
    if record_project is None or record_project == "":
        return False
    try:
        return Path(str(record_project)).resolve() == wanted.resolve()
    except OSError:
        return Path(str(record_project)) == wanted


def filter_records(
    records: list[dict[str, Any]],
    *,
    project: Path | None = None,
    since: date | None = None,
) -> list[dict[str, Any]]:
    wanted_project = project.resolve() if project is not None else None
    result: list[dict[str, Any]] = []
    for record in records:
        if wanted_project is not None and not _project_matches(
            record.get("project"), wanted_project
        ):
            continue
        if since is not None:
            recorded = _parse_recorded_date(record.get("recorded_at"))
            if recorded is None or recorded < since:
                continue
        result.append(record)
    return result


def _is_call_record(record: dict[str, Any]) -> bool:
    """True for per-call outcome lines (no event_type). Unknown types are skipped."""
    event_type = record.get("event_type")
    if event_type is None or event_type == "":
        return True
    # task_final, provider_switch, and any future/unknown event_type are not calls.
    return False


def _is_task_final(record: dict[str, Any]) -> bool:
    return record.get("event_type") == "task_final"


def _review_passed(verdict: Any) -> bool:
    if verdict is None:
        return False
    return str(verdict).strip().upper() == "PASS"


def _task_passed_on_attempt(
    calls: list[dict[str, Any]], attempt: int
) -> bool | None:
    """True/False when evidence exists; None when verification/review fields are unknown."""
    implement = [
        call
        for call in calls
        if call.get("phase") == "implement" and call.get("attempt") == attempt
    ]
    if not implement:
        return None
    verification_values = [call.get("verification_ok") for call in implement]
    if all(value is None for value in verification_values):
        verification_ok: bool | None = None
    else:
        verification_ok = any(value is True for value in verification_values)

    reviews = [
        call
        for call in calls
        if call.get("phase") == "review" and call.get("attempt") == attempt
    ]
    if not reviews:
        review_ok: bool | None = None
    else:
        verdicts = [call.get("review_verdict") for call in reviews]
        if all(value is None for value in verdicts):
            review_ok = None
        else:
            review_ok = any(_review_passed(value) for value in verdicts)

    if verification_ok is None and review_ok is None:
        return None
    if verification_ok is None:
        return bool(review_ok)
    if review_ok is None:
        return bool(verification_ok)
    return bool(verification_ok and review_ok)


def _label(model: str, effort: str) -> str:
    if effort == UNKNOWN:
        return model
    return f"{model}/{effort}"


def _fixer_labels(counter: Counter[tuple[str, str]]) -> list[str]:
    ordered = sorted(
        counter.items(), key=lambda item: (-item[1], item[0][0], item[0][1])
    )
    return [_label(model, effort) for (model, effort), _count in ordered]


def _first_attempt_passed_final(final: dict[str, Any], _triplet: tuple[str, str, str]) -> bool:
    """True only when the task finished DONE on a single implementation attempt.

    Same-model retries (attempt 2+ with the starting provider/model/effort) are
    not first-attempt passes — Decision 2 requires attempt-1 verification+review.
    """
    if str(final.get("final_status") or "") != "DONE":
        return False
    impl_attempts = final.get("implementation_attempts")
    if isinstance(impl_attempts, int):
        return impl_attempts == 1
    attempts = final.get("attempts")
    if isinstance(attempts, list):
        return len(attempts) == 1
    return False


def build_model_rows(records: list[dict[str, Any]]) -> tuple[list[ModelRow], list[str]]:
    calls_by_key: dict[ModelKey, int] = defaultdict(int)
    # triplet -> [pass booleans], one per started task / attempt-1 call with known outcome
    first_pass: dict[tuple[str, str, str], list[bool]] = defaultdict(list)
    started: dict[tuple[str, str, str], int] = defaultdict(int)
    done_attempts: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    done_tokens: dict[tuple[str, str, str], list[int]] = defaultdict(list)
    fixed_counts: dict[tuple[str, str, str], int] = defaultdict(int)
    fixer_for_start: dict[tuple[str, str, str], Counter[tuple[str, str]]] = defaultdict(
        Counter
    )
    triplets_with_finals: set[tuple[str, str, str]] = set()

    call_records = [record for record in records if _is_call_record(record)]
    finals = [record for record in records if _is_task_final(record)]

    for record in call_records:
        calls_by_key[_model_key(record)] += 1

    for final in finals:
        attempts = final.get("attempts")
        if not isinstance(attempts, list) or not attempts:
            continue
        first = attempts[0] if isinstance(attempts[0], dict) else None
        triplet = _attempt_triplet(first)
        triplets_with_finals.add(triplet)
        started[triplet] += 1
        first_pass[triplet].append(_first_attempt_passed_final(final, triplet))
        status = str(final.get("final_status") or "")
        if status != "DONE":
            continue
        impl_attempts = final.get("implementation_attempts")
        if isinstance(impl_attempts, int):
            done_attempts[triplet].append(impl_attempts)
        else:
            done_attempts[triplet].append(len(attempts))
        tokens = final.get("total_tokens")
        if tokens is not None:
            try:
                done_tokens[triplet].append(int(tokens))
            except (TypeError, ValueError):
                pass
        # Null/absent passing_attempt is unknown (Decision 4), not "fixed by other".
        passing = final.get("passing_attempt")
        if not isinstance(passing, dict):
            continue
        passing_triplet = _attempt_triplet(passing)
        if passing_triplet != triplet:
            fixed_counts[triplet] += 1
            fixer_for_start[triplet][(passing_triplet[1], passing_triplet[2])] += 1

    # Call-based first-attempt pass when no task_final covers that starting model.
    calls_by_task: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in call_records:
        task_id = record.get("task_id")
        if task_id is None or task_id == "":
            continue
        calls_by_task[(_field(record.get("project")), str(task_id))].append(record)

    for _task_key, task_calls in calls_by_task.items():
        attempt_ones = [
            call
            for call in task_calls
            if call.get("phase") == "implement" and call.get("attempt") == 1
        ]
        if not attempt_ones:
            continue
        triplet = _attempt_triplet(attempt_ones[0])
        if triplet in triplets_with_finals:
            continue
        passed = _task_passed_on_attempt(task_calls, 1)
        if passed is None:
            continue
        first_pass[triplet].append(bool(passed))

    row_keys: set[ModelKey] = set(calls_by_key)
    for provider, model, effort in set(started) | set(first_pass):
        row_keys.add(
            ModelKey(provider=provider, model=model, effort=effort, phase="implement")
        )

    rows: list[ModelRow] = []
    for key in sorted(
        row_keys,
        key=lambda item: (item.provider, item.model, item.effort, item.phase),
    ):
        triplet = (key.provider, key.model, key.effort)
        pass_list = first_pass.get(triplet, [])
        attempt_list = done_attempts.get(triplet, [])
        token_list = done_tokens.get(triplet, [])
        rows.append(
            ModelRow(
                provider=key.provider,
                model=key.model,
                effort=key.effort,
                phase=key.phase,
                calls=calls_by_key.get(key, 0),
                first_attempt_passes=sum(1 for value in pass_list if value),
                first_attempt_total=len(pass_list),
                avg_attempts_to_done=(
                    sum(attempt_list) / len(attempt_list) if attempt_list else None
                ),
                tokens_per_done=(
                    sum(token_list) / len(token_list) if token_list else None
                ),
                fixed_by_other=fixed_counts.get(triplet, 0),
                started_tasks=started.get(triplet, 0),
            )
        )

    suggestions: list[str] = []
    seen_labels: set[str] = set()
    for triplet in sorted(started.keys(), key=lambda item: (item[1], item[2], item[0])):
        _provider, model, effort = triplet
        total = started[triplet]
        if total < _SUGGESTION_MIN_TASKS:
            continue
        label = _label(model, effort)
        if label in seen_labels:
            continue
        seen_labels.add(label)
        pass_list = first_pass.get(triplet, [])
        passes = sum(1 for value in pass_list if value)
        fixed = fixed_counts.get(triplet, 0)
        if fixed <= 0:
            continue
        fixers = _fixer_labels(fixer_for_start[triplet])
        if not fixers:
            continue
        if len(fixers) == 1:
            fixer_text = fixers[0]
        elif len(fixers) == 2:
            fixer_text = f"{fixers[0]} or {fixers[1]}"
        else:
            fixer_text = ", ".join(fixers[:-1]) + f", or {fixers[-1]}"
        suggestions.append(
            f"{label}: {passes} of {total} first attempts passed; "
            f"{fixed} were fixed by {fixer_text}. "
            f"Consider starting with {fixers[0]}."
        )

    return rows, suggestions


def format_models_report(rows: list[ModelRow], suggestions: list[str]) -> str:
    if not rows:
        return "No outcomes recorded yet."
    header = (
        f"{'provider':<10} {'model':<22} {'effort':<10} {'phase':<12} "
        f"{'calls':>5} {'first_pass':>10} {'avg_att':>7} "
        f"{'tokens/done':>11} {'fixed':>9}"
    )
    lines = [header]
    for row in rows:
        if row.first_attempt_total:
            first = f"{row.first_attempt_passes}/{row.first_attempt_total}"
        else:
            first = "-"
        avg = (
            f"{row.avg_attempts_to_done:.1f}"
            if row.avg_attempts_to_done is not None
            else "-"
        )
        tokens = (
            f"{row.tokens_per_done:.0f}" if row.tokens_per_done is not None else "-"
        )
        if row.started_tasks:
            fixed = f"{row.fixed_by_other}/{row.started_tasks}"
        else:
            fixed = "-"
        lines.append(
            f"{row.provider[:10]:<10} {row.model[:22]:<22} {row.effort[:10]:<10} "
            f"{row.phase[:12]:<12} {row.calls:5d} {first:>10} {avg:>7} "
            f"{tokens:>11} {fixed:>9}"
        )
    if suggestions:
        lines.append("")
        lines.append("suggestions:")
        lines.extend(suggestions)
    return "\n".join(lines) + "\n"


def render_models_report(
    path: Path | None = None,
    *,
    project: Path | None = None,
    since: str | date | None = None,
) -> str:
    log_path = path if path is not None else default_outcomes_path()
    if not log_path.exists() or log_path.stat().st_size == 0:
        return "No outcomes recorded yet."
    since_date: date | None
    if since is None or since == "":
        since_date = None
    elif isinstance(since, datetime):
        since_date = since.date()
    elif isinstance(since, date):
        since_date = since
    else:
        since_date = date.fromisoformat(str(since)[:10])
    records = filter_records(
        load_outcome_records(log_path), project=project, since=since_date
    )
    if not records:
        if project is not None or since_date is not None:
            return "No matching outcomes."
        return "No outcomes recorded yet."
    rows, suggestions = build_model_rows(records)
    if not rows:
        return "No outcomes recorded yet."
    return format_models_report(rows, suggestions)
