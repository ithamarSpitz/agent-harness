"""Pure provider selection from a snapshot of current usage limits."""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import isfinite


@dataclass(frozen=True, slots=True)
class Choice:
    """The provider selected for one agent call and the data behind the choice."""

    provider: str | None
    reason: str
    scores: dict[str, float | None] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ClaudeReserve:
    """Maximum Claude utilization the harness may consume past."""

    five_hour_max_used_percent: float = 50.0
    weekly_max_used_percent: float = 100.0


@dataclass(frozen=True, slots=True)
class _Capacity:
    free: float | None
    reset_at: datetime | None
    urgency: float | None
    window: str | None
    unavailable: str | None = None


_WINDOW_CONTAINER_KEYS = ("windows", "rate_limits", "rateLimits", "limits")
_WINDOW_KEYS = (
    "five_hour",
    "fiveHour",
    "5h",
    "primary",
    "weekly",
    "seven_day",
    "sevenDay",
    "secondary",
    "monthly",
    "api",
)
_PERCENT_KEYS = (
    "used_percent",
    "used_percentage",
    "usedPercent",
    "percent_used",
    "utilization",
)
_RESET_KEYS = ("resets_at", "resetsAt", "reset_at", "resetAt")


def _value(source: object, *names: str) -> object | None:
    if isinstance(source, Mapping):
        for name in names:
            if name in source:
                return source[name]
        return None
    for name in names:
        if hasattr(source, name):
            return getattr(source, name)
    return None


def _number(value: object) -> float | None:
    try:
        result = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return result if isfinite(result) else None


def _datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        try:
            result = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            numeric = _number(value)
            if numeric is None:
                return None
            result = datetime.fromtimestamp(numeric, tz=timezone.utc)
    else:
        numeric = _number(value)
        if numeric is None or numeric <= 0:
            return None
        # Accept milliseconds as well as the seconds emitted by limits.py.
        if numeric > 100_000_000_000:
            numeric /= 1000.0
        try:
            result = datetime.fromtimestamp(numeric, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def _provider_snapshot(snapshot: object, provider: str) -> object | None:
    return _value(snapshot, provider)


def adapt_headroom_snapshot(snapshot: object) -> dict[str, dict[str, object]]:
    """Convert ``headroom_snapshot`` output into this module's window shape."""

    if not isinstance(snapshot, Mapping):
        return {}
    adapted: dict[str, dict[str, object]] = {}
    for provider, raw_entries in snapshot.items():
        if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)):
            continue
        windows: dict[str, object] = {}
        for entry in raw_entries:
            window = _value(entry, "window")
            used = _value(entry, "used_percent")
            if not isinstance(window, str):
                continue
            windows[window] = {
                "used_percent": used
                if bool(_value(entry, "known")) and _number(used) is not None
                else None,
                "resets_at": _value(entry, "resets_at"),
            }
        adapted[str(provider)] = {"windows": windows}
    return adapted


def _windows(provider_snapshot: object) -> list[tuple[str, object]]:
    container = _value(provider_snapshot, *_WINDOW_CONTAINER_KEYS)
    if container is None:
        container = provider_snapshot
    found: list[tuple[str, object]] = []
    if isinstance(container, Mapping):
        for name, window in container.items():
            if _value(window, *_PERCENT_KEYS) is not None:
                found.append((str(name), window))
        # Some snapshots use a single window object instead of a named map.
        if not found and _value(container, *_PERCENT_KEYS) is not None:
            found.append(("quota", container))
    else:
        for name in _WINDOW_KEYS:
            window = _value(container, name)
            if window is not None and _value(window, *_PERCENT_KEYS) is not None:
                found.append((name, window))
    return found


def _canonical_window(name: str) -> str:
    normalized = name.lower().replace("-", "_")
    if normalized in {"five_hour", "fivehour", "5h", "primary"}:
        return "5-hour"
    if normalized in {"weekly", "seven_day", "sevenday", "secondary"}:
        return "week"
    if normalized in {"monthly", "api"}:
        return "month"
    return name.replace("_", " ")


def _reserve_limit(reserve: object, name: str, default: float) -> float:
    value = _number(_value(reserve, name))
    return default if value is None else value


def _capacity(
    provider: str,
    provider_snapshot: object | None,
    *,
    claude_reserve: object,
    now: datetime,
) -> _Capacity:
    if provider_snapshot is None:
        return _Capacity(None, None, None, None)
    windows = _windows(provider_snapshot)
    if not windows:
        return _Capacity(None, None, None, None)

    parsed: list[tuple[str, float, datetime | None]] = []
    for name, window in windows:
        used = _number(_value(window, *_PERCENT_KEYS))
        if used is None:
            continue
        parsed.append((name, used, _datetime(_value(window, *_RESET_KEYS))))
    if not parsed:
        return _Capacity(None, None, None, None)

    if provider == "claude":
        five_hour_max = _reserve_limit(
            claude_reserve, "five_hour_max_used_percent", 50.0
        )
        weekly_max = _reserve_limit(
            claude_reserve, "weekly_max_used_percent", 100.0
        )
        for name, used, _ in parsed:
            canonical = _canonical_window(name)
            maximum = five_hour_max if canonical == "5-hour" else weekly_max
            if canonical in {"5-hour", "week"} and used >= maximum:
                return _Capacity(
                    0.0,
                    None,
                    None,
                    canonical,
                    f"reserve reached ({used:g}% used, max {maximum:g}%)",
                )

    # The highest-used window is the tightest, even when another resets sooner.
    name, used, reset_at = max(parsed, key=lambda item: item[1])
    free = 100.0 - used
    if free <= 0:
        return _Capacity(free, reset_at, None, _canonical_window(name), "out")
    if reset_at is None:
        return _Capacity(free, None, None, _canonical_window(name))
    hours = max((reset_at - now).total_seconds() / 3600.0, 0.5)
    return _Capacity(free, reset_at, free / hours, _canonical_window(name))


def _is_enabled(enabled: object, provider: str) -> bool:
    if isinstance(enabled, Mapping):
        value = enabled.get(provider, False)
        configured = _value(value, "enabled")
        return bool(value if configured is None else configured)
    if isinstance(enabled, (Sequence, Set)) and not isinstance(enabled, (str, bytes)):
        return provider in enabled
    return bool(_value(enabled, provider))


def _provider_order(enabled: object, priority: Sequence[str]) -> list[str]:
    """Return priority order followed by enabled providers omitted from it."""

    providers = list(priority)
    if isinstance(enabled, Mapping):
        providers.extend(str(provider) for provider in enabled)
    elif isinstance(enabled, (Sequence, Set)) and not isinstance(enabled, (str, bytes)):
        providers.extend(str(provider) for provider in enabled)
    else:
        providers.extend(("codex", "cursor", "claude"))
    return list(dict.fromkeys(providers))


def _disabled(disabled_for_task: object, provider: str) -> bool:
    if isinstance(disabled_for_task, Mapping):
        return bool(disabled_for_task.get(provider, False))
    if isinstance(disabled_for_task, (Sequence, Set)) and not isinstance(
        disabled_for_task, (str, bytes)
    ):
        return provider in disabled_for_task
    return False


def _hours_label(reset_at: datetime | None, now: datetime) -> str:
    if reset_at is None:
        return "at an unknown time"
    hours = max(0.0, (reset_at - now).total_seconds() / 3600.0)
    if hours < 1:
        return f"in {max(1, round(hours * 60))}m"
    return f"in {hours:.1f}h" if hours < 10 else f"in {hours:.0f}h"


def _available_reason(provider: str, capacity: _Capacity, now: datetime) -> str:
    if capacity.free is None:
        return f"{provider}: headroom unknown"
    reset = _hours_label(capacity.reset_at, now)
    score = "unknown" if capacity.urgency is None else f"{capacity.urgency:.1f}"
    return (
        f"{provider}: {capacity.free:g}% of the {capacity.window or 'quota'} free, "
        f"resets {reset} (urgency {score})"
    )


def _until_label(reset_at: datetime | None) -> str:
    if reset_at is None:
        return ""
    return f" until {reset_at.astimezone().strftime('%H:%M')}"


def _unavailable_reason(provider: str, capacity: _Capacity | None, why: str = "") -> str:
    if why:
        return f"{provider} {why}"
    if capacity is None:
        return f"{provider} unavailable"
    if capacity.unavailable:
        return f"{provider} {capacity.unavailable}{_until_label(capacity.reset_at)}"
    return f"{provider} unavailable"


def choose_provider(
    snapshot: object,
    *,
    phase: str,
    model_class: str,
    enabled: object,
    priority: Sequence[str],
    implementer_provider: str | None,
    disabled_for_task: object,
    claude_reserve: object,
    now: datetime,
) -> Choice:
    """Choose a provider without reading state or performing any I/O.

    ``model_class == "auto"`` identifies Cursor Auto's first implementation
    attempt. Later attempts carry their configured non-Auto model class.
    """

    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    ordered = _provider_order(enabled, priority)
    candidates = [
        provider
        for provider in ordered
        if _is_enabled(enabled, provider) and not _disabled(disabled_for_task, provider)
    ]

    if phase == "implement" and model_class == "auto" and "cursor" in candidates:
        return Choice("cursor", "cursor/auto: free first implementation attempt", {"cursor": None})

    capacities = {
        provider: _capacity(
            provider,
            _provider_snapshot(snapshot, provider),
            claude_reserve=claude_reserve,
            now=now,
        )
        for provider in candidates
    }
    available: list[str] = []
    for provider in candidates:
        free = capacities[provider].free
        if free is None or free > 0:
            available.append(provider)

    # Auto is a free implementation-only route, not a review provider. A
    # cursor snapshot can explicitly mark itself as Auto-only.
    if phase == "review" and "cursor" in available:
        cursor_snapshot = _provider_snapshot(snapshot, "cursor")
        auto_only = bool(_value(cursor_snapshot, "auto_only", "autoOnly"))
        if model_class == "auto" or auto_only:
            available.remove("cursor")
            capacities["cursor"] = _Capacity(0.0, None, None, None, "Auto only")

    if phase == "review" and implementer_provider in available and len(available) > 1:
        available.remove(implementer_provider)

    scores = {provider: capacities[provider].urgency for provider in ordered if provider in capacities}
    if available:
        known = [provider for provider in available if capacities[provider].urgency is not None]
        # Prefer measured urgency. Unknown headroom and equal scores fall back
        # to priority order (`known` / `available` are already priority-ordered).
        chosen = (
            max(known, key=lambda provider: capacities[provider].urgency or 0.0)
            if known
            else available[0]
        )
        reason = _available_reason(chosen, capacities[chosen], now)
        if phase == "review" and implementer_provider in capacities and chosen != implementer_provider:
            reason += f"; {implementer_provider} implemented"
        return Choice(chosen, reason, scores)

    reasons: list[str] = []
    for provider in ordered:
        if not _is_enabled(enabled, provider):
            reasons.append(_unavailable_reason(provider, None, "disabled"))
        elif _disabled(disabled_for_task, provider):
            reasons.append(_unavailable_reason(provider, None, "disabled for task"))
        elif phase == "review" and provider == "cursor" and model_class == "auto":
            reasons.append(_unavailable_reason(provider, None, "Auto cannot review"))
        else:
            reasons.append(_unavailable_reason(provider, capacities.get(provider)))
    return Choice(None, "; ".join(reasons) or "no providers configured", scores)
