"""The source contract.

One protocol, deliberately: fetching and parsing are separate so that parsing
is a pure function of stored bytes.  ``fetch_raw`` touches the network and
returns captured responses; ``parse`` turns those responses into normalized
rows and never performs I/O.  Replay is then just ``parse`` over responses
loaded from disk, which is what the regression tests do.

Adapters do not keep health state.  The collector derives health from what
actually happened during a run, so there is one implementation of that logic
instead of one per adapter.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, Sequence, runtime_checkable

from src.raw_store import RawResponse
from src.schema import Quote


@dataclass(frozen=True)
class Rejection:
    """A record that should have parsed but did not.

    Rejections are failures — they mean the source offered something in scope
    that this parser could not faithfully represent.  They are counted and
    stored so a format change shows up as a spike rather than as silence.
    """

    source: str
    reason: str
    """Stable machine-readable code, e.g. ``"unknown_team"``."""
    detail: str
    context: dict[str, Any] = field(default_factory=dict)


@dataclass
class ParseOutcome:
    """Everything a parse produced: rows, failures, and deliberate omissions."""

    quotes: list[Quote] = field(default_factory=list)
    rejections: list[Rejection] = field(default_factory=list)
    skipped: Counter[str] = field(default_factory=Counter)
    """Counts of *out-of-scope* input, keyed by reason.  Player props, futures
    and pitcher-conditional markets are expected and not errors — but they are
    counted so "we collected everything" is never assumed."""

    def extend(self, other: ParseOutcome) -> None:
        self.quotes.extend(other.quotes)
        self.rejections.extend(other.rejections)
        self.skipped.update(other.skipped)

    def reject(self, source: str, reason: str, detail: str, **context: Any) -> None:
        self.rejections.append(Rejection(source=source, reason=reason, detail=detail, context=context))

    @property
    def event_keys(self) -> set[str]:
        return {quote.event_key for quote in self.quotes}


@dataclass(frozen=True)
class SourceHealth:
    """Outcome of one source's participation in one collection run."""

    source_key: str
    ok: bool
    checked_at: datetime
    request_count: int = 0
    raw_bytes: int = 0
    latency_ms: float | None = None
    quote_count: int = 0
    event_count: int = 0
    rejection_count: int = 0
    skipped_count: int = 0
    unchanged_payloads: int = 0
    error_kind: str | None = None
    error_message: str | None = None

    def summary(self) -> str:
        if not self.ok:
            return f"{self.source_key}: FAILED [{self.error_kind}] {self.error_message}"
        parts = [
            f"{self.quote_count} quotes",
            f"{self.event_count} events",
            f"{self.request_count} requests",
            f"{self.raw_bytes / 1024:.0f} KiB",
        ]
        if self.latency_ms is not None:
            parts.append(f"{self.latency_ms:.0f} ms")
        if self.rejection_count:
            parts.append(f"{self.rejection_count} REJECTED")
        if self.skipped_count:
            parts.append(f"{self.skipped_count} out of scope")
        if self.unchanged_payloads:
            parts.append(
                f"{self.unchanged_payloads}/{self.request_count} payloads byte-identical to "
                "the previous run"
            )
        return f"{self.source_key}: OK ({', '.join(parts)})"


@runtime_checkable
class OddsSource(Protocol):
    """A sportsbook that can supply pregame game markets.

    One adapter per **book**, not per book-and-sport: the adapter owns the book's
    HTTP session, its raw-capture conventions and its parsing rules, and is
    parameterized by which leagues to collect.  Splitting it per sport would
    duplicate all of that three to six times over and open a new session per
    sport for no benefit.
    """

    @property
    def source_key(self) -> str:
        """Stable identifier, e.g. ``"pinnacle"``."""
        ...

    @property
    def leagues(self) -> tuple[str, ...]:
        """Canonical league keys this instance is configured to collect.

        The collector reports coverage against this, so a league that is
        configured but returns nothing is visible as a gap rather than being
        indistinguishable from one that was never requested.
        """
        ...

    def fetch_raw(self) -> list[RawResponse]:
        """Perform the network calls and return every captured response.

        Must raise a :class:`src.sources.guards.SourceError` subclass on
        blocked, empty, non-JSON, or structurally changed responses rather
        than returning nothing.
        """
        ...

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        """Turn captured responses into normalized rows.  No I/O."""
        ...

    def close(self) -> None:
        """Release held resources."""
        ...
