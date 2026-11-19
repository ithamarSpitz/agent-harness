"""Parsing and fault classification for Kiro's stream-json output."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class KiroStream:
    final_text: str
    status: str | None
    stop_reason: str | None
    error: str | None
    credits: float
    denied_tools: int
    finished: bool


@dataclass(slots=True)
class KiroFlags:
    quota: bool
    auth: bool
    model_unavailable: bool
    capacity: bool
    transient: bool
    config: bool


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_string(value: Any) -> str | None:
    return value if isinstance(value, str) else None


def parse_kiro_stream(text: str) -> KiroStream:
    """Parse Kiro JSON Lines output without performing any process handling."""
    chunks: list[str] = []
    final_text: str | None = None
    status: str | None = None
    stop_reason: str | None = None
    error: str | None = None
    credits = 0.0
    denied_tools = 0
    finished = False
    use_chunks = True

    for line in text.splitlines():
        if line.startswith("[denied]"):
            denied_tools += 1
            continue

        try:
            event = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")
        data = _mapping(event.get("data"))

        if event_type == "sessionUpdate":
            update = _mapping(data.get("update"))
            if update.get("sessionUpdate") == "agent_message_chunk":
                content = _mapping(update.get("content"))
                chunk = content.get("text")
                if isinstance(chunk, str):
                    chunks.append(chunk)
        elif event_type == "metadata":
            metering = data.get("meteringUsage")
            if isinstance(metering, list):
                for item_value in metering:
                    item = _mapping(item_value)
                    value = item.get("value")
                    if (
                        item.get("unit") == "credit"
                        and isinstance(value, (int, float))
                        and not isinstance(value, bool)
                    ):
                        credits += float(value)
        elif event_type == "runError":
            message = data.get("message")
            if isinstance(message, str):
                error = message
        elif event_type == "runFinished":
            finished = True
            status = _optional_string(data.get("status"))
            stop_reason = _optional_string(data.get("stopReason"))
            candidate = data.get("finalText")
            use_chunks = data.get("finalTextTruncated") is True or not isinstance(
                candidate, str
            )
            if not use_chunks:
                final_text = candidate

    if use_chunks or final_text is None:
        final_text = "".join(chunks)

    return KiroStream(
        final_text=final_text,
        status=status,
        stop_reason=stop_reason,
        error=error,
        credits=credits,
        denied_tools=denied_tools,
        finished=finished,
    )


def classify_kiro(
    stream: KiroStream, returncode: int, raw_output: str
) -> KiroFlags:
    """Classify a failed Kiro call, applying fault precedence from the spec."""
    flags = KiroFlags(
        quota=False,
        auth=False,
        model_unavailable=False,
        capacity=False,
        transient=False,
        config=False,
    )
    if returncode == 0 and stream.status == "success" and stream.error is None:
        return flags

    combined = f"{stream.error or ''}\n{raw_output}".casefold()
    matches: tuple[tuple[str, tuple[str, ...]], ...] = (
        (
            "quota",
            (
                "out of credits",
                "credit limit",
                "insufficient credits",
                "usage limit",
                "monthly limit",
                "quota",
                "limit reached",
                "upgrade your plan",
            ),
        ),
        (
            "auth",
            (
                "not logged in",
                "kiro-cli login",
                "please log in",
                "login required",
                "unauthorized",
                "401",
                "token expired",
                "session expired",
            ),
        ),
        ("model_unavailable", ("the model '", "is not available")),
        ("config", ("unexpected argument", "unknown option", "invalid value")),
        (
            "capacity",
            ("throttl", "too many requests", "429", "rate limit", "capacity"),
        ),
        (
            "transient",
            (
                "service unavailable",
                "503",
                "502",
                "internal server error",
                "timed out",
                "connection",
                "network",
            ),
        ),
    )
    for name, patterns in matches:
        if name == "model_unavailable":
            matched = all(pattern in combined for pattern in patterns)
        else:
            matched = any(pattern in combined for pattern in patterns)
        if matched:
            setattr(flags, name, True)
            break
    return flags
