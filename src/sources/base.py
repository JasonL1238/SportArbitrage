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
from typing import TYPE_CHECKING, Any, Mapping, Protocol, Sequence, runtime_checkable

from src.raw_store import RawResponse
from src.schema import Market, Quote

if TYPE_CHECKING:  # pragma: no cover - imported for typing only
    # Imported lazily: ``src.sources._common`` pulls in httpx, and the contract
    # this module states must be readable without a network stack present.
    from src.sources._common import Tier


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
    repaired: Counter[str] = field(default_factory=Counter)
    """Counts of rows that were **published after a field was reconstructed**.

    Neither a skip nor a rejection: the row is in :attr:`quotes`.  These were
    being counted in :attr:`skipped`, which the dashboard renders as "seen but
    out of scope" and whose note read "so the row is not kept" — the exact
    opposite of what happens.  118 Kambi rows across the two tenants were
    reported as discarded and were in fact published, carrying a derived
    American price because the feed's own two formats disagreed.

    Worth counting on its own: a venue whose formats stop agreeing is a
    data-quality signal, and burying it in a bucket labelled "not collected"
    loses it twice over."""

    def extend(self, other: ParseOutcome) -> None:
        self.quotes.extend(other.quotes)
        self.rejections.extend(other.rejections)
        self.skipped.update(other.skipped)
        self.repaired.update(other.repaired)

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
    repaired_count: int = 0
    unchanged_payloads: int = 0
    error_kind: str | None = None
    error_message: str | None = None
    scopes_requested: int = 0
    """How many scopes this source asked for, so a refusal has a denominator."""
    failed_scopes: tuple[str, ...] = ()
    """Scopes this source asked for and was refused, on a run it survived.

    A source that loses one league and keeps the rest is neither healthy nor
    failed, and reporting it as either is wrong.  Before this it read as
    healthy: the refusal reached a log line and stopped there, so a book that
    lost 14% of its rows to a 429 summarised as ``OK``.
    """

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
        if self.failed_scopes:
            parts.append(f"{len(self.failed_scopes)} SCOPE(S) REFUSED")
        if self.rejection_count:
            parts.append(f"{self.rejection_count} REJECTED")
        if self.skipped_count:
            parts.append(f"{self.skipped_count} out of scope")
        if self.repaired_count:
            parts.append(f"{self.repaired_count} repaired")
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

    def capabilities(self, *, tier: "Tier" = ...) -> Mapping[str, frozenset[Market]]:
        """Markets this instance claims to price, per league, at the full-game window.

        Declaring this is what lets coverage reporting say "this source has no
        totals for soccer" instead of inferring it from an absence — and it stops
        validation faulting a book for a market it never claimed.  The claim
        depends on the *tier*, because deferring a per-event call genuinely
        collects less: FanDuel's soccer spreads and totals exist only on its
        event page, so a ``core`` pass claims the moneyline alone and an absence
        of the other two is a request budget rather than a renamed label.
        """
        ...

    def fetch_raw(self, *, tier: "Tier" = ...) -> list[RawResponse]:
        """Perform the network calls and return every captured response.

        Must raise a :class:`src.sources.guards.SourceError` subclass on
        blocked, rate-limited, geo-restricted, empty, non-JSON, or structurally
        changed responses rather than returning nothing.

        *tier* is a **request budget**, not a market filter.  A source with no
        per-event follow-up returns the same responses for both tiers and says so
        in its docstring; one that has such a follow-up omits it under ``core``
        and narrows :meth:`capabilities` to match, so that the smaller row set is
        a stated consequence rather than a mystery.
        """
        ...

    def parse(self, raws: Sequence[RawResponse]) -> ParseOutcome:
        """Turn captured responses into normalized rows.  No I/O."""
        ...

    def close(self) -> None:
        """Release held resources."""
        ...
