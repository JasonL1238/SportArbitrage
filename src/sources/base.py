"""Source adapter protocol and common data structures.

Every odds source (paid API, public page, exchange feed) implements
``SourceAdapter`` so the scanner can treat them uniformly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from src.models import Event, PriceQuote


@dataclass
class SourceSnapshot:
    """Raw fetch result before parsing."""

    source_key: str
    sport_key: str
    raw_payload: str
    fetched_at: datetime
    url: str | None = None
    status_code: int | None = None
    headers_json: str | None = None
    content_type: str | None = None
    parser_version: str = "1"
    parse_error: str | None = None
    credits_used: int | None = None
    credits_remaining: int | None = None


@dataclass
class SourceHealth:
    """Point-in-time health report for a source adapter."""

    source_key: str
    is_healthy: bool
    last_success: datetime | None = None
    last_failure: datetime | None = None
    error_message: str | None = None
    latency_ms: float | None = None
    fetch_count: int = 0
    failure_count: int = 0


@runtime_checkable
class SourceAdapter(Protocol):
    """Protocol that every odds source must satisfy."""

    @property
    def source_key(self) -> str:
        """Unique identifier for this source (e.g. ``"odds_api"``)."""
        ...

    def discover_events(self, sport_key: str) -> list[dict[str, Any]]:
        """Return raw event metadata for *sport_key* (lightweight, no odds)."""
        ...

    def fetch_odds(self, sport_key: str) -> SourceSnapshot:
        """Fetch a full odds snapshot for *sport_key*."""
        ...

    def parse_events(self, raw: list[dict[str, Any]]) -> list[Event]:
        """Turn raw JSON structures into normalised ``Event`` models."""
        ...

    def healthcheck(self) -> SourceHealth:
        """Return current health status."""
        ...

    def supports_delta(self) -> bool:
        """Whether the source supports incremental (delta) updates."""
        ...

    def confidence_score(self) -> float:
        """0.0–1.0 reliability score for the data this source provides."""
        ...

    def close(self) -> None:
        """Release any held resources (HTTP clients, sockets, etc.)."""
        ...


@runtime_checkable
class QuoteSourceAdapter(Protocol):
    """Protocol for sources that emit normalized market-data quotes directly."""

    @property
    def source_key(self) -> str:
        """Unique source identifier."""
        ...

    def fetch_quotes(self, **kwargs: Any) -> list[PriceQuote]:
        """Fetch fresh normalized quotes."""
        ...

    def healthcheck(self) -> SourceHealth:
        """Return current health status."""
        ...

    def close(self) -> None:
        """Release held resources."""
        ...
