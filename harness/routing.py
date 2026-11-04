"""Pure provider selection from a snapshot of current usage limits."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass, field
from datetime import datetime, timezone
from math import inf, isfinite


@dataclass(frozen=True, slots=True)
class Choice:
    """The provider selected for one agent call and the data behind the choice.

    ``scores`` holds each provider's waste (percent that would go unused), or
    ``None`` when it is unknown or the provider has no usage. ``auto`` is true
    when Cursor was chosen to run Auto.
    """

    provider: str | None
    reason: str
    scores: dict[str, float | None] = field(default_factory=dict)
    auto: bool = False


@dataclass(frozen=True, slots=True)
class ClaudeReserve:
    """Claude caps: the 5-hour cap rises with the hours until another provider returns."""

    min_percent: float = 60.0
    max_percent: float = 90.0
    min_hours: float = 1.0
    max_hours: float = 5.0
    weekly_percent_per_day: float = 7.0


PaceFn = Callable[[str, str], float]
DEFAULT_PACE: dict[str, float] = {"5-hour": 30.0, "week": 3.0, "month": 1.0}
FALLBACK_PACE = 3.0


@dataclass(frozen=True, slots=True)
class _Window:
    name: str
    used: float
    reset_at: datetime | None


@dataclass(frozen=True, slots=True)
class _Usage:
    windows: tuple[_Window, ...] = ()

    @property
    def known(self) -> bool:
        return bool(self.windows)

    @property
    def blocking(self) -> list[_Window]:
        return [w for w in self.windows if w.used >= 100]


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


def _usage(provider_snapshot: object | None) -> _Usage:
    if provider_snapshot is None:
        return _Usage()
    parsed: list[_Window] = []
    for name, window in _windows(provider_snapshot):
        used = _number(_value(window, *_PERCENT_KEYS))
        if used is None:
            continue
        parsed.append(
            _Window(_canonical_window(name), used, _datetime(_value(window, *_RESET_KEYS)))
        )
    return _Usage(tuple(parsed))


def _reserve_limit(reserve: object, name: str, default: float) -> float:
    value = _number(_value(reserve, name))
    return default if value is None else value


def claude_five_hour_cap(hours: float, reserve: object = None) -> float:
    """Claude 5-hour cap (percent used) when another provider returns in ``hours``."""

    low = _reserve_limit(reserve, "min_percent", 60.0)
    high = _reserve_limit(reserve, "max_percent", 90.0)
    min_h = _reserve_limit(reserve, "min_hours", 1.0)
    max_h = _reserve_limit(reserve, "max_hours", 5.0)
    if hours <= min_h:
        return low
    if hours >= max_h:
        return high
    return low + (hours - min_h) / (max_h - min_h) * (high - low)


def claude_weekly_cap(days: float, reserve: object = None) -> float:
    """Claude weekly cap (percent used) with ``days`` left until the weekly reset."""

    per_day = _reserve_limit(reserve, "weekly_percent_per_day", 7.0)
    return min(100.0, max(0.0, 100.0 - per_day * max(0.0, days)))


def _hours_until(moment: datetime, now: datetime) -> float:
    return max(0.0, (moment - now).total_seconds() / 3600.0)


def _hours_until_other_usage(
    others: Sequence[str],
    usage: Mapping[str, _Usage],
    disabled: Mapping[str, bool],
    now: datetime,
) -> float:
    """Hours until the first other provider has usage again (0 if one has it now)."""

    best = inf
    for provider in others:
        blocking = usage[provider].blocking
        if not blocking:
            if not disabled[provider]:
                return 0.0
            continue
        resets = [w.reset_at for w in blocking]
        if any(reset is None for reset in resets):
            continue
        latest = max(reset for reset in resets if reset is not None)
        best = min(best, _hours_until(latest, now))
    return best


def _claude_block(
    u: _Usage, *, hours: float, reserve: object, now: datetime
) -> tuple[str | None, float]:
    """Return (why Claude is over a cap, or None) and the 5-hour cap in force."""

    cap = claude_five_hour_cap(hours, reserve)
    for window in u.windows:
        if window.name == "5-hour" and window.used >= cap:
            return f"5-hour at {window.used:g}% (cap {cap:g}%)", cap
        if window.name == "week":
            # Unknown reset: strictest weekly cap, as if the whole week is left.
            days = 7.0 if window.reset_at is None else _hours_until(window.reset_at, now) / 24.0
            weekly = claude_weekly_cap(days, reserve)
            if window.used >= weekly:
                return f"weekly at {window.used:g}% (cap {weekly:g}%)", cap
    return None, cap


def _waste(provider: str, u: _Usage, pace: PaceFn, now: datetime) -> tuple[float, str | None]:
    """Highest waste over the windows of a provider, and the window it came from."""

    best, label = 0.0, None
    for window in u.windows:
        if window.reset_at is None:
            continue
        left = 100.0 - window.used
        value = max(
            0.0, left - _hours_until(window.reset_at, now) * pace(provider, window.name)
        )
        if value > best:
            best, label = value, window.name
    return best, label


def _default_pace(_provider: str, window: str) -> float:
    return DEFAULT_PACE.get(window, FALLBACK_PACE)


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


def _until_label(reset_at: datetime | None) -> str:
    if reset_at is None:
        return ""
    return f" until {reset_at.astimezone().strftime('%H:%M')}"


def _pools_exhausted(u: _Usage) -> bool:
    return len(u.windows) >= 2 and all(w.used >= 100 for w in u.windows)


def choose_provider(
    snapshot: object,
    *,
    phase: str,
    model_class: str = "",
    enabled: object,
    priority: Sequence[str],
    implementer_provider: str | None,
    disabled_for_task: object,
    claude_reserve: object = None,
    now: datetime,
    pace: PaceFn | None = None,
    auto_when_exhausted: bool = False,
) -> Choice:
    """Choose a provider without reading state or performing any I/O.

    The order is ``priority``; providers whose quota would otherwise go to waste
    go first (highest waste first). With ``auto_when_exhausted`` and both Cursor
    pools at 100%, an implementation goes to Cursor Auto before anything else.
    Claude is capped by limits that depend on when another provider has usage
    again. ``model_class`` is accepted for compatibility and ignored.
    """

    del model_class
    now = now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now.astimezone(timezone.utc)
    pace_of = pace or _default_pace
    ordered = [p for p in _provider_order(enabled, priority) if _is_enabled(enabled, p)]
    task_disabled = {p: _disabled(disabled_for_task, p) for p in ordered}
    usage = {p: _usage(_provider_snapshot(snapshot, p)) for p in ordered}
    candidates = [p for p in ordered if not task_disabled[p]]
    scores: dict[str, float | None] = {p: None for p in ordered}

    if (
        phase == "implement"
        and auto_when_exhausted
        and "cursor" in candidates
        and _pools_exhausted(usage["cursor"])
    ):
        return Choice("cursor", "cursor auto: both pools at 100%", scores, auto=True)

    hours = 0.0
    if "claude" in candidates:
        others = [p for p in ordered if p != "claude"]
        hours = _hours_until_other_usage(others, usage, task_disabled, now)

    notes: dict[str, str] = {}
    blocked: dict[str, str] = {}
    has_usage: list[str] = []
    for provider in candidates:
        u = usage[provider]
        if u.blocking:
            reset = max((w.reset_at for w in u.blocking if w.reset_at is not None), default=None)
            blocked[provider] = f"{provider} out{_until_label(reset)}"
            continue
        if provider == "claude":
            why, cap = _claude_block(u, hours=hours, reserve=claude_reserve, now=now)
            if why:
                blocked[provider] = f"claude {why}"
                continue
            notes[provider] = f"claude under cap {cap:g}%"
        has_usage.append(provider)

    if phase == "review":
        # Auto never reviews; pools at 100% already mean Cursor has no usage.
        if implementer_provider in has_usage and len(has_usage) > 1:
            has_usage.remove(implementer_provider)

    wastes: dict[str, tuple[float, str | None]] = {}
    for provider in has_usage:
        if usage[provider].known:
            wastes[provider] = _waste(provider, usage[provider], pace_of, now)
            scores[provider] = wastes[provider][0]
    urgent = sorted(
        (p for p in has_usage if wastes.get(p, (0.0, None))[0] > 0),
        key=lambda p: -wastes[p][0],
    )
    final = urgent + [p for p in has_usage if p not in urgent]

    if final:
        chosen = final[0]
        if chosen in urgent:
            reason = f"urgent: waste {wastes[chosen][0]:.3g}% of {chosen} {wastes[chosen][1]}"
        else:
            reason = "order"
        if chosen in notes:
            reason += f"; {notes[chosen]}"
        if phase == "review" and implementer_provider in usage and chosen != implementer_provider:
            reason += f"; {implementer_provider} implemented"
        return Choice(chosen, reason, scores)

    reasons: list[str] = []
    for provider in _provider_order(enabled, priority):
        if not _is_enabled(enabled, provider):
            reasons.append(f"{provider} disabled")
        elif task_disabled.get(provider):
            reasons.append(f"{provider} disabled for task")
        elif provider in blocked:
            reasons.append(blocked[provider])
        else:
            reasons.append(f"{provider} unavailable")
    return Choice(None, "; ".join(reasons) or "no providers configured", scores)
