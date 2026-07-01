"""Central registry of source adapters."""
from __future__ import annotations

import logging
from typing import Iterator

from src.sources.base import SourceAdapter

log = logging.getLogger(__name__)


class SourceRegistry:
    """Manages a named collection of :class:`SourceAdapter` instances."""

    def __init__(self) -> None:
        self._adapters: dict[str, SourceAdapter] = {}

    def register(self, adapter: SourceAdapter) -> None:
        key = adapter.source_key
        if key in self._adapters:
            log.warning("Replacing existing adapter for source %r", key)
        self._adapters[key] = adapter
        log.info("Registered source adapter: %s", key)

    def get(self, key: str) -> SourceAdapter:
        try:
            return self._adapters[key]
        except KeyError:
            raise KeyError(f"No adapter registered for source {key!r}") from None

    def all(self) -> list[SourceAdapter]:
        return list(self._adapters.values())

    def healthy(self) -> list[SourceAdapter]:
        return [a for a in self._adapters.values() if a.healthcheck().is_healthy]

    def keys(self) -> list[str]:
        return list(self._adapters.keys())

    def close_all(self) -> None:
        for adapter in self._adapters.values():
            try:
                adapter.close()
            except Exception:
                log.exception("Error closing adapter %s", adapter.source_key)
        self._adapters.clear()

    def __len__(self) -> int:
        return len(self._adapters)

    def __contains__(self, key: str) -> bool:
        return key in self._adapters

    def __iter__(self) -> Iterator[SourceAdapter]:
        return iter(self._adapters.values())
