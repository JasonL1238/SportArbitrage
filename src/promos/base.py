"""Promo-source contract — parallel to :mod:`src.sources.base`, not a Quote path.

Fetch and parse stay split so regression tests can replay stored bytes.  Promo
adapters never emit :class:`~src.schema.Quote` rows and are never registered in
:data:`src.sources.registry.SOURCES`.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence, runtime_checkable

from src.promos.schema import PromoOffer
from src.raw_store import RawResponse
from src.sources.base import Rejection


@dataclass
class PromoParseOutcome:
    """Everything a promo parse produced."""

    offers: list[PromoOffer] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    #: A plain Counter, deliberately outside the odds pipeline's ReasonCounter
    #: guarantee: every promo skip site increments by a constant 1, so the
    #: computed-zero door that class exists to close has no promo instance.
    #: A promo parser that starts incrementing by a computed count should
    #: switch this to ReasonCounter in the same change.
    skipped: Counter[str] = field(default_factory=Counter)

    def extend(self, other: "PromoParseOutcome") -> None:
        self.offers.extend(other.offers)
        self.rejections.extend(other.rejections)
        self.skipped.update(other.skipped)

    def reject(self, source: str, reason: str, detail: str, **context: Any) -> None:
        self.rejections.append(
            Rejection(source=source, reason=reason, detail=detail, context=context)
        )


@dataclass(frozen=True)
class PromoSourceHealth:
    """Outcome of one promo source in one collection pass."""

    source_key: str
    ok: bool
    checked_at: datetime
    request_count: int = 0
    raw_bytes: int = 0
    latency_ms: float | None = None
    offer_count: int = 0
    rejection_count: int = 0
    skipped_count: int = 0
    error_kind: str | None = None
    error_message: str | None = None

    def summary(self) -> str:
        if not self.ok:
            return f"{self.source_key}: FAILED [{self.error_kind}] {self.error_message}"
        parts = [
            f"{self.offer_count} offers",
            f"{self.request_count} requests",
            f"{self.raw_bytes / 1024:.0f} KiB",
        ]
        if self.latency_ms is not None:
            parts.append(f"{self.latency_ms:.0f} ms")
        if self.rejection_count:
            parts.append(f"{self.rejection_count} REJECTED")
        if self.skipped_count:
            parts.append(f"{self.skipped_count} skipped")
        return f"{self.source_key}: OK ({', '.join(parts)})"


@runtime_checkable
class PromoSource(Protocol):
    """A sportsbook (or exchange) that publishes promotions we can scrape."""

    @property
    def source_key(self) -> str: ...

    def fetch_raw(self) -> list[RawResponse]:
        """Network I/O.  Raises :class:`~src.sources.guards.SourceError` on refusal."""
        ...

    def parse(self, raws: Sequence[RawResponse]) -> PromoParseOutcome:
        """Pure parse of captured bytes — no I/O, no clock."""
        ...

    def close(self) -> None: ...


__all__ = [
    "PromoParseOutcome",
    "PromoSource",
    "PromoSourceHealth",
]
