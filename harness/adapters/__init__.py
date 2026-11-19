from __future__ import annotations

from collections.abc import Iterator

from .base import (
    BaseAdapter,
    CallContext,
    Fault,
    FindResult,
    Levels,
    ProviderAdapter,
    classify_result,
    command_version,
)


class AdapterRegistry:
    def __init__(self) -> None:
        self._adapters: dict[str, ProviderAdapter] = {}

    def register(self, adapter: ProviderAdapter) -> None:
        if adapter.name in self._adapters:
            raise ValueError(f"adapter already registered: {adapter.name}")
        self._adapters[adapter.name] = adapter

    def get(self, name: str) -> ProviderAdapter | None:
        return self._adapters.get(name)

    def names(self) -> list[str]:
        return list(self._adapters)

    def enabled(self) -> list[ProviderAdapter]:
        return [adapter for adapter in self._adapters.values() if adapter.enabled()]

    def __iter__(self) -> Iterator[ProviderAdapter]:
        return iter(self._adapters.values())


__all__ = [
    "AdapterRegistry",
    "BaseAdapter",
    "CallContext",
    "Fault",
    "FindResult",
    "Levels",
    "ProviderAdapter",
    "classify_result",
    "command_version",
]
